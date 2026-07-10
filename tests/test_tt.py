#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Unit tests for TokenTrim -- standard library only.

Run from the repo root:
    python -m unittest discover -s tests -v
"""
import io
import json
import os
import sys
import tempfile
import time
import unittest

# Isolate tt's on-disk state (stats/tee/cache) BEFORE importing it, and make
# the repo root importable.
_TMP = tempfile.mkdtemp(prefix="tt_test_")
os.environ["TT_HOME"] = _TMP
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tt  # noqa: E402


def saved_pct(raw, compact):
    r, c = tt.est_tokens(raw), tt.est_tokens(compact)
    return 100.0 * (r - c) / max(1, r)


class TestHelpers(unittest.TestCase):
    def test_est_tokens(self):
        self.assertEqual(tt.est_tokens(""), 0)
        self.assertEqual(tt.est_tokens("abcd"), 1)
        self.assertEqual(tt.est_tokens("abcde"), 2)

    def test_strip_ansi(self):
        self.assertEqual(tt.strip_ansi("\x1b[31mred\x1b[0m"), "red")

    def test_strip_progress(self):
        text = "Downloading foo 1/3\n50%\nspin\r spin\r done\nreal line"
        out = tt.strip_progress(text)
        self.assertNotIn("Downloading", out)
        self.assertNotIn("50%", out)
        self.assertIn(" done", out)   # final state of the \r redraw survives
        self.assertIn("real line", out)

    def test_dedup_consecutive(self):
        out = tt.dedup_consecutive("a\na\na\nb")
        self.assertEqual(out, "a  (x3)\nb")

    def test_truncate_lines(self):
        text = "\n".join("l%d" % i for i in range(200))
        out = tt.truncate_lines(text, 30)
        self.assertIn("omitted by tokentrim", out)
        self.assertIn("l0", out)
        self.assertIn("l199", out)
        self.assertLessEqual(len(out.split("\n")), 31)

    def test_error_lines_only(self):
        text = "fine\nError: boom\nfine again\nall ok"
        out = tt.error_lines_only(text)
        self.assertEqual(out, "Error: boom")

    def test_error_keywords_match_cross_marks(self):
        # These symbols were mojibake-corrupted in an earlier build.
        self.assertTrue(tt.ERROR_KEYWORDS.search("  ✗ test_login"))
        self.assertTrue(tt.ERROR_KEYWORDS.search("  ✖ 3 problems"))


class TestRedaction(unittest.TestCase):
    def test_json_skeleton_redacts_secret_keys(self):
        obj = {"password": "hunter2", "name": "app",
               "nested": {"apiKey": "abc123", "count": 3}}
        skel = tt.json_skeleton(obj)
        self.assertEqual(skel["password"], "<redacted>")
        self.assertEqual(skel["nested"]["apiKey"], "<redacted>")
        self.assertEqual(skel["name"], "app")

    def test_json_skeleton_collapses_lists_and_long_strings(self):
        skel = tt.json_skeleton({"items": [1, 2, 3], "s": "x" * 100})
        self.assertEqual(skel["items"], [1, "...(3 items)"])
        self.assertTrue(skel["s"].endswith("..."))

    def test_redact_secrets_assignments(self):
        out = tt.redact_secrets("PASSWORD=hunter2 user=bob")
        self.assertNotIn("hunter2", out)
        self.assertIn("user=bob", out)
        out = tt.redact_secrets('api_key: "abc-def-123"')
        self.assertNotIn("abc-def-123", out)

    def test_redact_secrets_token_formats(self):
        for token in ("AKIAIOSFODNN7EXAMPLE",
                      "ghp_" + "a" * 36,
                      "xoxb-123456789012-abcdef"):
            self.assertNotIn(token, tt.redact_secrets("x " + token + " y"))

    def test_redact_secrets_leaves_normal_text(self):
        text = "def get_user(name):\n    return db.find(name)"
        self.assertEqual(tt.redact_secrets(text), text)

    def test_auto_compress_handles_bom(self):
        # PowerShell pipes often prepend a UTF-8 BOM; JSON detection (and
        # therefore redaction) must still work.
        blob = "﻿" + json.dumps({"password": "hunter2", "a": 1})
        compact, redacted = tt.auto_compress(blob)
        self.assertTrue(redacted)
        self.assertNotIn("hunter2", compact)

    def test_trim_generic_text_is_redacted(self):
        compact, redacted = tt.auto_compress(
            "starting up\nDB_PASSWORD=supersecret123\nready")
        self.assertNotIn("supersecret123", compact)
        self.assertTrue(redacted)


class TestDiff(unittest.TestCase):
    DIFF = (
        "diff --git a/app.py b/app.py\n"
        "index 111..222 100644\n"
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -1,7 +1,7 @@ def main():\n"
        " context1\n"
        " context2\n"
        "-old line\n"
        "+new line\n"
        " context3\n"
        " context4\n"
        " context5\n"
    )

    def test_compress_diff_drops_context(self):
        out = tt._compress_diff(self.DIFF)
        self.assertIn("app.py  (+1 -1)", out)
        self.assertIn("+new line", out)
        self.assertIn("-old line", out)
        self.assertNotIn("context1", out)
        self.assertNotIn("index 111", out)
        # hunk header kept, trailing context stripped
        self.assertIn("@@ -1,7 +1,7 @@", out)
        self.assertNotIn("def main", out)

    def test_auto_compress_detects_diff(self):
        compact, _ = tt.auto_compress(self.DIFF)
        self.assertIn("1 file(s) changed", compact)

    def test_empty_diff(self):
        self.assertEqual(tt._compress_diff(""), "no changes")


class TestTerraform(unittest.TestCase):
    PLAN = json.dumps({"resource_changes": [
        {"address": "aws_s3_bucket.data",
         "change": {"actions": ["create"], "before": None,
                    "after": {"name": "data-bucket"}}},
        {"address": "aws_db_instance.main",
         "change": {"actions": ["update"],
                    "before": {"size": "small", "password": "old"},
                    "after": {"size": "large", "password": "new"}}},
        {"address": "aws_iam_user.legacy",
         "change": {"actions": ["delete"], "before": {}, "after": None}},
        {"address": "aws_vpc.main",
         "change": {"actions": ["no-op"], "before": {}, "after": {}}},
    ]})

    def test_plan_summary(self):
        out = tt._tf_resource_changes(self.PLAN, {})
        self.assertIn("Plan: 1 add, 1 change, 1 destroy", out)
        self.assertIn("+ aws_s3_bucket.data", out)
        self.assertIn("~ aws_db_instance.main", out)
        self.assertIn("- aws_iam_user.legacy", out)
        self.assertNotIn("no-op", out)

    def test_plan_redacts_secret_attrs(self):
        out = tt._tf_resource_changes(self.PLAN, {})
        self.assertIn("password: <redacted>", out)
        self.assertNotIn("old", out.split("password")[1].split("\n")[0])

    def test_resource_include_filter(self):
        cfg = {"resource_include": ["aws_s3_bucket"]}
        out = tt._tf_resource_changes(self.PLAN, cfg)
        self.assertIn("aws_s3_bucket.data", out)
        self.assertNotIn("aws_db_instance", out)


class TestKubectl(unittest.TestCase):
    HEALTHY = (
        "NAME      READY   STATUS    RESTARTS   AGE\n"
        "web-1     1/1     Running   0          2d\n"
        "web-2     1/1     Running   0          2d\n"
    )
    BROKEN = (
        "NAME      READY   STATUS             RESTARTS   AGE\n"
        "web-1     1/1     Running            0          2d\n"
        "app-x     0/1     CrashLoopBackOff   7          45m\n"
    )

    def test_all_healthy_one_line(self):
        out = tt._kubectl_pods_summary(self.HEALTHY)
        self.assertEqual(out, "2 pods | 2 Running")

    def test_anomaly_surfaced(self):
        out = tt._kubectl_pods_summary(self.BROKEN)
        self.assertIn("! app-x  CrashLoopBackOff  restarts=7  age=45m", out)
        self.assertIn("2 pods", out)


class TestCloudPresets(unittest.TestCase):
    def test_az_aks(self):
        obj = {"name": "aks-prod", "location": "westeurope",
               "kubernetesVersion": "1.29.2",
               "provisioningState": "Succeeded",
               "powerState": {"code": "Running"},
               "agentPoolProfiles": [
                   {"name": "system", "count": 3,
                    "vmSize": "Standard_D4s_v5", "osType": "Linux",
                    "mode": "System"}],
               "identity": {"type": "SystemAssigned"},
               "networkProfile": {"networkPlugin": "azure"}}
        out = tt._az_aks(obj)
        self.assertIn("AKS aks-prod | westeurope | k8s 1.29.2", out)
        self.assertIn("system(3xD4s_v5,Linux,System)", out)

    def test_sagemaker(self):
        obj = {"TrainingJobName": "xgb-01", "TrainingJobStatus": "Completed",
               "BillableTimeInSeconds": 3720,
               "ResourceConfig": {"InstanceType": "ml.m5.xlarge"},
               "FinalMetricDataList": [
                   {"MetricName": "validation:auc", "Value": 0.9134}]}
        out = tt._aws_sagemaker_training(obj)
        self.assertIn("xgb-01 | Completed | 1h2m billable | ml.m5.xlarge", out)
        self.assertIn("validation:auc=0.9134", out)

    def test_az_apim_state_is_rstripped(self):
        out = tt._az_apim([{"name": "orders", "path": "orders", "state": ""}])
        self.assertIn("\n  orders orders", out)
        self.assertFalse(out.split("\n")[1].endswith(" "))


class TestTables(unittest.TestCase):
    def test_compact_table_drops_columns(self):
        text = ("CONTAINER ID  IMAGE     STATUS\n"
                "abc123def456  nginx     Up 2 hours\n"
                "789ghi012jkl  redis     Up 5 days\n")
        out = tt.compact_table(text, drop_headers=["CONTAINER ID"])
        self.assertNotIn("abc123def456", out)
        self.assertIn("nginx", out)


class TestML(unittest.TestCase):
    def _keras_log(self, epochs=100):
        lines = []
        for e in range(1, epochs + 1):
            loss = 1.0 / e
            val = 0.05 + abs(e - 41) * 0.001
            lines.append("Epoch %d/%d" % (e, epochs))
            lines.append("loss: %.4f - val_loss: %.4f" % (loss, val))
        return "\n".join(lines)

    def test_training_summary(self):
        log = self._keras_log()
        out = tt._compress_training(log)
        self.assertIn("Training 100 epochs", out)
        self.assertIn("Best: epoch 41", out)
        self.assertIn("No improvement last 59 epochs", out)
        self.assertGreater(saved_pct(log, out), 95)

    def test_auto_compress_detects_training(self):
        compact, _ = tt.auto_compress(self._keras_log(10))
        self.assertIn("Training", compact)

    def test_pandas_info(self):
        text = ("<class 'pandas.core.frame.DataFrame'>\n"
                "RangeIndex: 184320 entries, 0 to 184319\n"
                "Data columns (total 3 columns):\n"
                " #   Column   Non-Null Count   Dtype\n"
                " 0   id       184320 non-null  int64\n"
                " 1   price    120000 non-null  float64\n"
                " 2   name     184320 non-null  object\n"
                "dtypes: float64(1), int64(1), object(1)\n"
                "memory usage: 30.9 MB\n")
        out = tt._compress_pandas_info(text)
        self.assertIn("DF 184320 rows x 3 cols", out)
        self.assertIn("price(-64320!)", out)   # >25% missing flagged
        self.assertNotIn("id(", out)           # no nulls -> not listed

    def test_classification_report(self):
        text = ("              precision    recall  f1-score   support\n"
                "\n"
                "         cat       0.90      0.95      0.92       100\n"
                "         dog       0.80      0.60      0.69        50\n"
                "\n"
                "    accuracy                           0.87       150\n"
                "   macro avg       0.85      0.78      0.81       150\n"
                "weighted avg       0.87      0.87      0.86       150\n")
        out = tt._compress_classification_report(text)
        self.assertIn("accuracy 0.87", out)
        self.assertIn("worst recall: dog", out)


class TestTrace(unittest.TestCase):
    def test_stack_trace_kept(self):
        trace = (
            "Traceback (most recent call last):\n"
            '  File "app.py", line 10, in <module>\n'
            "    main()\n"
            '  File "app.py", line 7, in main\n'
            "    1 / 0\n"
            "ZeroDivisionError: division by zero\n")
        compact, _ = tt.auto_compress(trace)
        self.assertIn("ZeroDivisionError", compact)
        self.assertIn('File "app.py", line 7', compact)
        self.assertNotIn("1 / 0", compact)  # source snippet dropped


class TestJsonAuto(unittest.TestCase):
    def test_json_detected_and_redacted(self):
        blob = json.dumps({"instances": [{"id": "i-1", "secretAccessKey":
                                          "AAAA"}] * 20})
        compact, redacted = tt.auto_compress(blob)
        self.assertTrue(redacted)
        self.assertNotIn("AAAA", compact)
        self.assertIn("(20 items)", compact)
        self.assertGreater(saved_pct(blob, compact), 80)


class TestDedupMessages(unittest.TestCase):
    def test_journal_style_dedup(self):
        import re
        prefix = re.compile(r"^\w{3}\s+\d+\s[\d:]+\s\S+\s")
        text = ("Jan 01 10:00:01 host app[1]: connection refused\n"
                "Jan 01 10:00:02 host app[1]: connection refused\n"
                "Jan 01 10:00:03 host app[1]: connection refused\n"
                "Jan 01 10:00:04 host app[1]: started\n")
        out = tt.dedup_messages(text, prefix)
        self.assertIn("connection refused  (x3)", out)
        self.assertIn("started", out)


class TestSafety(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows-only: .cmd shim resolution")
    def test_capture_resolves_cmd_shims(self):
        # npm/tsc/etc. are .cmd shims on Windows; bare-name spawn fails
        # without PATH+PATHEXT resolution (real bug found in benchmarks).
        d = tempfile.mkdtemp(prefix="tt_cmd_")
        shim = os.path.join(d, "fakecmd.cmd")
        with open(shim, "w") as fh:
            fh.write("@echo hello from shim\n")
        old = os.environ["PATH"]
        os.environ["PATH"] = d + os.pathsep + old
        try:
            code, out = tt.capture(["fakecmd"])
        finally:
            os.environ["PATH"] = old
        self.assertEqual(code, 0)
        self.assertIn("hello from shim", out)

    def test_capture_command_not_found(self):
        code, out = tt.capture(["definitely-not-a-command-xyz"])
        self.assertEqual(code, 127)
        self.assertIn("not found", out)

    def test_filter_cat_missing_file_sets_exit_code(self):
        res = tt.filter_cat(["cat", "no_such_file_xyz.txt"], {})
        self.assertEqual(res.code, 1)

    def test_codeact_grep_and_view(self):
        d = tempfile.mkdtemp(prefix="tt_ca_")
        p = os.path.join(d, "mod.py")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("import os\n\ndef hello():\n    # TODO: fix\n    pass\n")
        hits = tt._ca_grep("TODO", d)
        self.assertIn("1 matches in 1 files", hits)
        sig = tt._ca_view(p, sig=True)
        self.assertIn("def hello():", sig)
        self.assertNotIn("pass", sig)


class TestSmartDedup(unittest.TestCase):
    def test_timestamped_consecutive(self):
        text = "\n".join(
            "2026-07-09T10:00:%02dZ ERROR connection refused" % i
            for i in range(5))
        out = tt.smart_dedup(text)
        self.assertEqual(len(out.split("\n")), 1)
        self.assertIn("(x5)", out)

    def test_interleaved_repeats(self):
        lines = []
        for i in range(4):
            lines.append("10:00:%02d worker heartbeat ok" % (i * 2))
            lines.append("10:00:%02d job-%d done" % (i * 2 + 1, i))
        out = tt.smart_dedup("\n".join(lines))
        self.assertIn("heartbeat ok  (x4 total)", out)
        self.assertEqual(out.count("heartbeat"), 1)
        self.assertIn("job-2 done", out)  # unique lines all survive

    def test_docker_iso_timestamps(self):
        text = "\n".join(
            "2026-01-01T00:00:%02d.123456789Z app | request handled" % i
            for i in range(50))
        out = tt.smart_dedup(text)
        self.assertEqual(len(out.split("\n")), 1)
        self.assertIn("(x50)", out)

    def test_no_timestamps_still_works(self):
        out = tt.smart_dedup("a\na\na\nb")
        self.assertEqual(out, "a  (x3)\nb")


class TestBudget(unittest.TestCase):
    def test_under_budget_untouched(self):
        self.assertEqual(tt.enforce_budget("short", 100), "short")

    def test_over_budget_capped(self):
        text = "\n".join("line %d with some padding text" % i
                         for i in range(500))
        out = tt.enforce_budget(text, 100)
        self.assertLessEqual(tt.est_tokens(out), 100)
        self.assertIn("--budget", out)

    def test_zero_budget_disabled(self):
        self.assertEqual(tt.enforce_budget("anything", 0), "anything")

    def test_parse_globals_budget(self):
        opts, rest = tt.parse_globals(["--budget", "200", "git", "status"])
        self.assertEqual(opts["budget"], 200)
        self.assertEqual(rest, ["git", "status"])
        opts, rest = tt.parse_globals(["--budget=300", "ls"])
        self.assertEqual(opts["budget"], 300)
        self.assertEqual(rest, ["ls"])


class TestFoldPrefix(unittest.TestCase):
    def test_folds_common_dirs(self):
        pre, short = tt._fold_prefix(["src/app/a.py", "src/app/b.py",
                                      "src/app/sub/c.py"])
        self.assertEqual(pre, "src/app/")
        self.assertEqual(short, ["a.py", "b.py", "sub/c.py"])

    def test_no_fold_for_shallow_or_few(self):
        pre, _ = tt._fold_prefix(["a.py", "b.py", "c.py"])
        self.assertEqual(pre, "")
        pre, _ = tt._fold_prefix(["src/a.py", "src/b.py"])
        self.assertEqual(pre, "")

    def test_windows_separators(self):
        pre, short = tt._fold_prefix(["src\\app\\a.py", "src\\app\\b.py",
                                      "src\\app\\c.py"])
        self.assertEqual(pre, "src/app/")
        self.assertEqual(short, ["a.py", "b.py", "c.py"])


class TestPytestPreset(unittest.TestCase):
    OUTPUT = (
        "==================== test session starts ====================\n"
        "collected 3 items\n"
        "\n"
        "tests/test_app.py .F.                                 [100%]\n"
        "\n"
        "========================= FAILURES ==========================\n"
        "________________________ test_login _________________________\n"
        "\n"
        "    def test_login():\n"
        "        resp = client.get('/login')\n"
        ">       assert resp.status_code == 200\n"
        "E       assert 404 == 200\n"
        "E        +  where 404 = <Response [404]>.status_code\n"
        "\n"
        "tests/test_app.py:14: AssertionError\n"
        "================== short test summary info ==================\n"
        "FAILED tests/test_app.py::test_login - assert 404 == 200\n"
        "=============== 1 failed, 2 passed in 0.12s =================\n")

    def test_failure_parsed(self):
        out = tt._compress_pytest(self.OUTPUT)
        self.assertIn("1 failed, 2 passed in 0.12s", out)
        self.assertIn("FAILED tests/test_app.py::test_login", out)
        self.assertIn("E       assert 404 == 200", out)
        self.assertIn("tests/test_app.py:14: AssertionError", out)
        # source snippet and pass markers are dropped
        self.assertNotIn("client.get", out)
        self.assertNotIn("[100%]", out)
        self.assertGreater(saved_pct(self.OUTPUT, out), 50)

    def test_not_pytest_returns_none(self):
        self.assertIsNone(tt._compress_pytest("hello\nworld"))

    def test_quiet_mode_summary(self):
        # pytest -q: final summary has no ===== decoration.
        out = tt._compress_pytest(
            "FAILED tests/test_x.py::test_a - assert 1 == 2\n"
            "3 failed, 10 passed in 0.08s\n")
        self.assertIn("3 failed, 10 passed in 0.08s", out)


class TestBuildPreset(unittest.TestCase):
    def test_maven_success(self):
        raw = ("[INFO] Scanning for projects...\n" +
               "[INFO] Downloading from central: ...\n" * 50 +
               "[INFO] BUILD SUCCESS\n"
               "[INFO] Total time:  12.345 s\n"
               "[INFO] ------------------------------------\n")
        out = tt._compress_build(raw, 0)
        self.assertIn("ok -- BUILD SUCCESS", out)
        self.assertIn("12.345 s", out)
        self.assertGreater(saved_pct(raw, out), 90)

    def test_maven_failure_keeps_error_lines(self):
        raw = ("[INFO] Compiling 42 source files\n"
               "[ERROR] /src/App.java:[10,5] cannot find symbol\n"
               "[ERROR] -> [Help 1]\n"
               "[INFO] BUILD FAILURE\n")
        out = tt._compress_build(raw, 1)
        self.assertIn("FAILED", out)
        self.assertIn("cannot find symbol", out)
        self.assertNotIn("Compiling 42", out)

    def test_gradle_success(self):
        out = tt._compress_build("> Task :compileJava\nBUILD SUCCESSFUL in 3s\n"
                                 "2 actionable tasks: 2 executed\n", 0)
        self.assertIn("ok -- BUILD SUCCESSFUL in 3s", out)


class TestLibFrames(unittest.TestCase):
    def test_library_frames_collapsed(self):
        trace = (
            "Traceback (most recent call last):\n"
            '  File "app.py", line 10, in <module>\n'
            '  File "/venv/lib/python3.11/site-packages/requests/api.py", line 59, in get\n'
            '  File "/venv/lib/python3.11/site-packages/requests/sessions.py", line 587, in request\n'
            '  File "/venv/lib/python3.11/site-packages/urllib3/conn.py", line 100, in urlopen\n'
            "ConnectionError: refused\n")
        out = tt._compress_trace(trace)
        self.assertIn("[3 library frames collapsed]", out)
        self.assertIn('File "app.py"', out)
        self.assertIn("ConnectionError", out)

    def test_single_lib_frame_kept(self):
        trace = ('  File "app.py", line 1, in x\n'
                 '  File "/x/site-packages/a.py", line 2, in y\n'
                 "ValueError: nope\n")
        out = tt._compress_trace(trace)
        self.assertIn("site-packages", out)


class TestHtmlAndBinary(unittest.TestCase):
    def test_html_to_text(self):
        html = ("<!doctype html><html><head><title>T</title>"
                "<script>var x=1;</script><style>.a{}</style></head>"
                "<body><h1>Hello</h1><p>World &amp; friends</p></body></html>")
        out = tt._html_to_text(html)
        self.assertIn("Hello", out)
        self.assertIn("World & friends", out)
        self.assertNotIn("var x", out)
        self.assertNotIn("<h1>", out)

    def test_looks_binary(self):
        self.assertTrue(tt._looks_binary("abc\x00def"))
        self.assertTrue(tt._looks_binary("iVBORw0KGgo" + "A" * 500))
        self.assertFalse(tt._looks_binary("normal log line\nanother line"))

    def test_auto_compress_suppresses_binary(self):
        compact, _ = tt.auto_compress("QmFzZTY0" * 200)
        self.assertIn("suppressed", compact)
        self.assertLess(len(compact), 200)


class TestCacheDiff(unittest.TestCase):
    def test_small_diff(self):
        old = "a\nb\nc"
        new = "a\nB\nc"
        diff = tt._small_diff(old, new)
        self.assertIn("-b", diff)
        self.assertIn("+B", diff)

    def test_identical_returns_none(self):
        self.assertIsNone(tt._small_diff("same", "same"))

    def test_huge_change_returns_none(self):
        old = "\n".join("l%d" % i for i in range(50))
        new = "\n".join("x%d" % i for i in range(50))
        self.assertIsNone(tt._small_diff(old, new))

    def test_cache_roundtrip_unchanged_then_diff(self):
        argv = ["fake", "cmd", str(time.time())]
        self.assertIsNone(tt.cache_check(argv, "out v1", allow_diff=True))
        note = tt.cache_check(argv, "out v1", allow_diff=True)
        self.assertIn("unchanged", note)
        note = tt.cache_check(argv, "out v2", allow_diff=True)
        self.assertIn("changed since", note)
        self.assertIn("+out v2", note)


class TestInteractiveGuard(unittest.TestCase):
    def test_interactive_detected(self):
        self.assertTrue(tt._needs_tty(["git", "rebase", "-i", "main"]))
        self.assertTrue(tt._needs_tty(["git", "commit"]))
        self.assertTrue(tt._needs_tty(["git", "add", "-p"]))
        self.assertTrue(tt._needs_tty(["docker", "exec", "-it", "web", "sh"]))
        self.assertTrue(tt._needs_tty(["kubectl", "edit", "deploy/x"]))
        self.assertTrue(tt._needs_tty(["az", "login"]))
        self.assertTrue(tt._needs_tty(["npm", "init"]))

    def test_non_interactive_passes(self):
        self.assertFalse(tt._needs_tty(["git", "commit", "-m", "msg"]))
        self.assertFalse(tt._needs_tty(["git", "commit", "-am", "msg"]))
        self.assertFalse(tt._needs_tty(["git", "commit", "--amend",
                                        "--no-edit"]))
        self.assertFalse(tt._needs_tty(["git", "status"]))
        self.assertFalse(tt._needs_tty(["docker", "ps"]))
        self.assertFalse(tt._needs_tty(["grep", "-i", "todo", "."]))


class TestShellInit(unittest.TestCase):
    def _capture(self, args):
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            tt.cmd_shell_init(args)
        return buf.getvalue()

    def test_powershell_functions(self):
        out = self._capture(["powershell"])
        self.assertIn("function global:git { & tt git @args }", out)
        self.assertIn("function global:kubectl", out)

    def test_bash_aliases(self):
        out = self._capture(["bash"])
        self.assertIn("alias git='tt git'", out)
        self.assertIn('eval "$(tt shell-init bash)"', out)


class TestHook(unittest.TestCase):
    def _run_hook(self, payload):
        import contextlib
        old_stdin = sys.stdin
        sys.stdin = io.StringIO(json.dumps(payload))
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                tt.cmd_hook(["claude"])
        finally:
            sys.stdin = old_stdin
        return buf.getvalue()

    def test_simple_command_rewritten(self):
        out = self._run_hook({"tool_name": "Bash",
                              "tool_input": {"command": "git status"}})
        data = json.loads(out)
        self.assertEqual(
            data["hookSpecificOutput"]["updatedInput"]["command"],
            "tt git status")

    def test_pipeline_untouched(self):
        out = self._run_hook({"tool_name": "Bash",
                              "tool_input": {"command": "git log | head -5"}})
        self.assertEqual(out, "")

    def test_already_tt_untouched(self):
        out = self._run_hook({"tool_name": "Bash",
                              "tool_input": {"command": "tt git status"}})
        self.assertEqual(out, "")

    def test_unknown_command_untouched(self):
        out = self._run_hook({"tool_name": "Bash",
                              "tool_input": {"command": "vim file.txt"}})
        self.assertEqual(out, "")


class TestInitMultiAgent(unittest.TestCase):
    def setUp(self):
        self._cwd = os.getcwd()
        self._dir = tempfile.mkdtemp(prefix="tt_init_")
        os.chdir(self._dir)

    def tearDown(self):
        os.chdir(self._cwd)

    def _read(self, rel):
        with open(os.path.join(self._dir, rel), encoding="utf-8") as fh:
            return fh.read()

    def test_claude_and_agents_files(self):
        import contextlib
        with contextlib.redirect_stdout(io.StringIO()):
            tt.cmd_init(["claude"])
            tt.cmd_init(["agents"])
        self.assertIn("TokenTrim", self._read("CLAUDE.md"))
        self.assertIn("TokenTrim", self._read("AGENTS.md"))

    def test_cursor_has_frontmatter(self):
        import contextlib
        with contextlib.redirect_stdout(io.StringIO()):
            tt.cmd_init(["cursor"])
        text = self._read(os.path.join(".cursor", "rules", "tokentrim.mdc"))
        self.assertTrue(text.startswith("---"))
        self.assertIn("alwaysApply: true", text)

    def test_idempotent(self):
        import contextlib
        with contextlib.redirect_stdout(io.StringIO()):
            tt.cmd_init(["claude"])
            tt.cmd_init(["claude"])
        self.assertEqual(self._read("CLAUDE.md").count("Managed by TokenTrim"),
                         1)

    def test_claude_hook_installed(self):
        import contextlib
        with contextlib.redirect_stdout(io.StringIO()):
            tt.cmd_init(["claude", "--hook"])
        cfgj = json.loads(self._read(os.path.join(".claude", "settings.json")))
        pre = cfgj["hooks"]["PreToolUse"]
        self.assertEqual(pre[0]["hooks"][0]["command"], "tt hook claude")
        # second run must not duplicate
        with contextlib.redirect_stdout(io.StringIO()):
            tt.cmd_init(["claude", "--hook"])
        cfgj = json.loads(self._read(os.path.join(".claude", "settings.json")))
        self.assertEqual(len(cfgj["hooks"]["PreToolUse"]), 1)


class TestMap(unittest.TestCase):
    def test_map_lists_signatures(self):
        import contextlib
        d = tempfile.mkdtemp(prefix="tt_map_")
        sub = os.path.join(d, "src")
        os.makedirs(sub)
        with open(os.path.join(sub, "core.py"), "w", encoding="utf-8") as fh:
            fh.write("import os\n\nclass Engine:\n    def start(self):\n"
                     "        pass\n\ndef helper():\n    return 1\n")
        with open(os.path.join(d, "notes.txt"), "w", encoding="utf-8") as fh:
            fh.write("hi\n")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            tt.cmd_map([d])
        out = buf.getvalue()
        self.assertIn("repo map:", out)
        self.assertIn("core.py", out)
        self.assertIn("class Engine:", out)
        self.assertIn("def helper():", out)
        self.assertIn("notes.txt", out)
        self.assertNotIn("import os", out)  # imports dropped at map level


if __name__ == "__main__":
    unittest.main(verbosity=2)
