#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TokenTrim (tt) - a zero-dependency CLI proxy that compresses the output of
common developer commands *before* it reaches an AI agent's context window.

Why: AI coding assistants now bill per token. Every `git diff`, `terraform plan`,
`docker logs`, `pytest`, etc. that the agent runs dumps its raw, noisy output
into the conversation and you pay for all of it. TokenTrim keeps the signal
(errors, changed lines, summaries) and drops the noise (progress bars,
boilerplate, repeated lines, unchanged context) -- typically 60-90% fewer
tokens with no loss of the information the agent actually needs.

Design goals:
  * Single file, Python standard library only (works on Python 3.8+).
  * Native on Windows / macOS / Linux -- no Rust, no Homebrew, no WSL, no uv.
  * NEVER change what a command does. tt only reshapes its *output*.
  * NEVER swallow a failure: on non-zero exit the full raw output is saved to
    disk and its path is reported, and the original exit code is preserved.
  * Fail safe: if a filter errors, fall back to the raw command output.

Usage:
    tt <command> [args...]     e.g. tt git diff, tt pytest, tt terraform plan
    tt gain                    show accumulated token savings
    tt init [--global]         write the GitHub Copilot integration file
    tt help
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time

try:
    from pathlib import Path
except Exception:  # pragma: no cover - Path exists on all supported versions
    Path = None

VERSION = "1.4.0"

# --------------------------------------------------------------------------- #
# Paths / state                                                               #
# --------------------------------------------------------------------------- #
HOME = Path(os.path.expanduser("~"))
TT_HOME = Path(os.environ.get("TT_HOME", str(HOME / ".tokentrim")))
TEE_DIR = TT_HOME / "tee"
STATS_FILE = TT_HOME / "stats.jsonl"
CONFIG_FILE = TT_HOME / "config.json"

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
SECRET_KEY_RE = re.compile(
    r"(secret|password|passwd|token|apikey|api[_-]?key|access[_-]?key|"
    r"accesskeyid|credential|private|policydocument|sessiontoken|"
    r"authorization|connectionstring)",
    re.IGNORECASE,
)

# Global caps so a single command can never blow up the context.
MAX_LINES = 60
MAX_LINE_LEN = 500


# --------------------------------------------------------------------------- #
# Small helpers                                                               #
# --------------------------------------------------------------------------- #
def est_tokens(text: str) -> int:
    """Rough token estimate (~4 chars/token). Good enough for savings stats."""
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


def load_config() -> dict:
    cfg = {"exclude": [], "tee_mode": "failures", "ultra": False,
           "resource_include": [], "resource_exclude": []}
    try:
        if CONFIG_FILE.exists():
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                cfg.update(data)
    except Exception:
        pass
    # Per-project overrides: .tokentrim.json in cwd or a parent (stop at the
    # repo root). Lets each repo set its own budget/excludes/resource filters.
    try:
        d = Path(os.getcwd())
        for _ in range(6):
            pc = d / ".tokentrim.json"
            if pc.exists():
                data = json.loads(pc.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    cfg.update(data)
                break
            if (d / ".git").exists() or d.parent == d:
                break
            d = d.parent
    except Exception:
        pass
    return cfg


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def strip_progress(text: str) -> str:
    """Drop carriage-return progress redraws and spinner/percent noise."""
    # Keep only the final state of any line that used \r to redraw in place.
    cleaned_lines = []
    for raw_line in text.split("\n"):
        if "\r" in raw_line:
            raw_line = raw_line.split("\r")[-1]
        cleaned_lines.append(raw_line)
    out = []
    for line in cleaned_lines:
        s = line.strip()
        if not s:
            out.append(line)
            continue
        # Common download / build progress patterns.
        if re.match(r"^[\.\#=>\-\s]*\d+%", s):
            continue
        if re.match(r"^(Downloading|Fetching|Receiving|Resolving|Unpacking|"
                    r"Delta compression|Compressing|Writing objects|"
                    r"Counting objects|Enumerating objects)\b", s):
            continue
        out.append(line)
    return "\n".join(out)


def dedup_consecutive(text: str) -> str:
    """Collapse consecutive identical lines into 'line  (xN)'."""
    lines = text.split("\n")
    out = []
    prev = None
    count = 0

    def flush():
        if prev is None:
            return
        if count > 1:
            out.append("%s  (x%d)" % (prev, count))
        else:
            out.append(prev)

    for line in lines:
        if line == prev:
            count += 1
        else:
            flush()
            prev = line
            count = 1
    flush()
    return "\n".join(out)


# Volatile prefixes that make identical log lines look different: ISO 8601,
# syslog (Jan  1 10:00:01), apache (01/Jan/2026:10:00:01) and bare times.
_TS_PREFIX_RE = re.compile(
    r"^[\[\(]?(?:"
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?"
    r"|\w{3}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}(?:\.\d+)?"
    r"|\d{2}/\w{3}/\d{4}[: ]\d{2}:\d{2}:\d{2}"
    r"|\d{2}:\d{2}:\d{2}(?:[.,]\d+)?"
    r")[\]\)]?[: ]*")


def smart_dedup(text: str, min_group: int = 4) -> str:
    """Collapse repeated log lines while ignoring timestamp prefixes.
    Consecutive repeats become 'line  (xN)'; lines that also repeat further
    down (interleaved A-B-A-B patterns) keep only their first occurrence,
    annotated with the total count."""
    lines = text.split("\n")
    keys = [_TS_PREFIX_RE.sub("", l, count=1) for l in lines]
    groups = []  # [first_line, key, consecutive_count]
    for l, k in zip(lines, keys):
        if groups and groups[-1][1] == k and k.strip():
            groups[-1][2] += 1
        else:
            groups.append([l, k, 1])
    totals = {}
    for _, k, c in groups:
        if k.strip():
            totals[k] = totals.get(k, 0) + c
    seen = set()
    out = []
    for l, k, c in groups:
        if k.strip() and totals[k] >= min_group and totals[k] > c:
            if k in seen:
                continue
            seen.add(k)
            out.append("%s  (x%d total)" % (l, totals[k]))
        elif c > 1:
            out.append("%s  (x%d)" % (l, c))
        else:
            out.append(l)
    return "\n".join(out)


def clip_line(line: str) -> str:
    if len(line) > MAX_LINE_LEN:
        return line[:MAX_LINE_LEN] + " ...[+%d chars]" % (len(line) - MAX_LINE_LEN)
    return line


def truncate_lines(text: str, max_lines: int = MAX_LINES) -> str:
    """Keep head + tail of long output, note how many lines were omitted."""
    lines = [clip_line(l) for l in text.split("\n")]
    if len(lines) <= max_lines:
        return "\n".join(lines)
    head = max_lines * 2 // 3
    tail = max_lines - head
    omitted = len(lines) - head - tail
    return "\n".join(
        lines[:head]
        + ["... [%d lines omitted by tokentrim] ..." % omitted]
        + lines[-tail:]
    )


def enforce_budget(text: str, budget_tokens: int) -> str:
    """Hard cap: shrink text until it fits the (approximate) token budget."""
    if budget_tokens <= 0 or est_tokens(text) <= budget_tokens:
        return text
    note = "[tt: output capped at ~%d tokens (--budget); " \
           "`tt --raw` for full output]" % budget_tokens
    for n in (40, 25, 15, 8, 4):
        candidate = truncate_lines(text, n)
        if est_tokens(candidate) + est_tokens(note) <= budget_tokens:
            return candidate + "\n" + note
    keep = max(0, budget_tokens * 4 - len(note) - 8)
    return text[:keep] + "\n...\n" + note


def _fold_prefix(paths):
    """If >=3 paths share a directory prefix of >=2 levels, return
    (prefix, shortened_paths); otherwise ('', normalized_paths).
    Saves repeating 'packages/frontend/src/...' on every line in monorepos."""
    norm = [p.replace("\\", "/") for p in paths]
    if len(norm) < 3:
        return "", norm
    parts = [p.split("/")[:-1] for p in norm]
    if not all(parts):
        return "", norm
    common = []
    for i, seg in enumerate(parts[0]):
        if all(len(q) > i and q[i] == seg for q in parts[1:]):
            common.append(seg)
        else:
            break
    if len(common) < 2:
        return "", norm
    pre = "/".join(common) + "/"
    return pre, [p[len(pre):] for p in norm]


def _html_to_text(html):
    """Very small HTML-to-text: drop script/style/tags, keep visible text."""
    t = re.sub(r"(?is)<(script|style|svg|head|noscript)[^>]*>.*?</\1>", " ", html)
    t = re.sub(r"(?s)<!--.*?-->", " ", t)
    t = re.sub(r"(?i)<br\s*/?>|</(p|div|h[1-6]|li|tr|section|article)>", "\n", t)
    t = re.sub(r"(?s)<[^>]+>", " ", t)
    for a, b in (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                 ("&quot;", '"'), ("&#39;", "'"), ("&nbsp;", " ")):
        t = t.replace(a, b)
    lines = [re.sub(r"[ \t]+", " ", l).strip() for l in t.split("\n")]
    return "\n".join(l for l in lines if l)


def _looks_binary(text, sample=4000):
    """True for content an LLM cannot use anyway: NULs, control-character
    soup, or long unbroken base64 runs (images, archives, minified blobs)."""
    s = text[:sample]
    if not s:
        return False
    if "\x00" in s:
        return True
    ctrl = sum(1 for ch in s if ord(ch) < 9 or 13 < ord(ch) < 32)
    if ctrl > len(s) * 0.05:
        return True
    if re.search(r"[A-Za-z0-9+/=]{400,}", s):
        return True
    return False


# ✗ / ✖ / × are the cross-mark symbols some test runners print.
ERROR_KEYWORDS = re.compile(
    r"\b(error|errors|failed|failure|fail|fatal|panic|exception|traceback|"
    r"assert|assertion|denied|refused|cannot|unable|invalid|not found|"
    r"unresolved|conflict|E\d{2,})\b|✗|✖|×",
    re.IGNORECASE,
)


def error_lines_only(text: str, context: int = 0) -> str:
    lines = text.split("\n")
    keep = set()
    for i, line in enumerate(lines):
        if ERROR_KEYWORDS.search(line):
            for j in range(max(0, i - context), min(len(lines), i + context + 1)):
                keep.add(j)
    if not keep:
        return ""
    return "\n".join(lines[i] for i in sorted(keep))


# key = value / key: value assignments with a secret-looking key, plus a few
# well-known token formats (AWS, GitHub, Slack, JWT, generic sk- keys).
_SECRET_ASSIGN_RE = re.compile(
    r"(?i)([A-Za-z0-9_.-]*(?:secret|password|passwd|pwd|token|apikey|"
    r"api[_-]?key|access[_-]?key|accountkey|sharedaccesskey|sessiontoken|"
    r"credential|authorization|bearer)[A-Za-z0-9_.-]*)(\s*[:=]\s*)"
    r"(\"[^\"]*\"|'[^']*'|\S+)")
_SECRET_TOKEN_RE = re.compile(
    r"\b(AKIA[0-9A-Z]{16}"
    r"|ghp_[A-Za-z0-9]{36}"
    r"|github_pat_[A-Za-z0-9_]{22,}"
    r"|xox[baprs]-[A-Za-z0-9-]{10,}"
    r"|sk-[A-Za-z0-9_-]{20,}"
    r"|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,})")


def redact_secrets(value: str) -> str:
    """Mask secret-looking assignments and well-known token formats in text."""
    if not value:
        return value
    out = _SECRET_ASSIGN_RE.sub(
        lambda m: m.group(1) + m.group(2) + "<redacted>", value)
    out = _SECRET_TOKEN_RE.sub("<redacted>", out)
    return out


def json_skeleton(obj, redact=True, _key=""):
    """Return structure with types/short values instead of full payloads."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if redact and SECRET_KEY_RE.search(str(k)):
                out[k] = "<redacted>"
            else:
                out[k] = json_skeleton(v, redact, str(k))
        return out
    if isinstance(obj, list):
        if not obj:
            return []
        return [json_skeleton(obj[0], redact, _key), "...(%d items)" % len(obj)] \
            if len(obj) > 1 else [json_skeleton(obj[0], redact, _key)]
    if isinstance(obj, str):
        if redact and SECRET_KEY_RE.search(_key):
            return "<redacted>"
        return obj if len(obj) <= 60 else obj[:57] + "..."
    return obj


# --------------------------------------------------------------------------- #
# Command execution                                                           #
# --------------------------------------------------------------------------- #
class Result:
    """Holds the outcome of running (and filtering) a command."""

    __slots__ = ("code", "compact", "raw", "label", "redacted")

    def __init__(self, code, compact, raw, label, redacted=False):
        self.code = code
        self.compact = compact
        self.raw = raw
        self.label = label
        # When True, `compact` had secrets stripped; never fall back to `raw`.
        self.redacted = redacted


def _resolve_exe(argv):
    """On Windows, commands like npm/tsc are .cmd/.bat shims that
    CreateProcess cannot spawn by bare name; resolve argv[0] via PATH
    (shutil.which honours PATHEXT) so they run exactly like in a shell."""
    if os.name == "nt" and argv:
        exe = shutil.which(argv[0])
        if exe:
            return [exe] + list(argv[1:])
    return argv


def capture(argv, extra_env=None):
    """Run argv, capture stdout+stderr merged, return (code, combined_text)."""
    argv = _resolve_exe(argv)
    env = os.environ.copy()
    env.setdefault("NO_COLOR", "1")
    env.setdefault("CLICOLOR", "0")
    env.setdefault("GIT_PAGER", "cat")
    env.setdefault("PAGER", "cat")
    if extra_env:
        env.update(extra_env)
    try:
        proc = subprocess.run(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            encoding="utf-8",
            errors="replace",  # never crash on odd bytes (Windows codepages)
        )
    except OSError as exc:
        # ENOENT (command not found), ELOOP, permission, etc. Never crash.
        if getattr(exc, "errno", None) == 2:
            return 127, "%s: command not found" % (argv[0] if argv else "")
        return 127, "%s: %s" % (argv[0] if argv else "", exc)
    except Exception as exc:  # pragma: no cover
        return 1, "tokentrim: could not run command: %s" % exc
    return proc.returncode, strip_ansi(proc.stdout or "")


def stream_passthrough(argv):
    """Run a command with inherited stdio -- 100% transparent, no capture."""
    try:
        proc = subprocess.run(_resolve_exe(argv))
        return proc.returncode
    except OSError as exc:
        if getattr(exc, "errno", None) == 2:
            sys.stderr.write("%s: command not found\n" % (argv[0] if argv else ""))
        else:
            sys.stderr.write("%s: %s\n" % (argv[0] if argv else "", exc))
        return 127
    except KeyboardInterrupt:  # pragma: no cover
        return 130


def tee_save(argv, raw_text):
    """Persist full raw output so nothing is ever lost on failure."""
    try:
        TEE_DIR.mkdir(parents=True, exist_ok=True)
        stamp = int(time.time())
        safe = re.sub(r"[^a-zA-Z0-9]+", "_", " ".join(argv[:3]))[:40]
        path = TEE_DIR / ("%d_%s.log" % (stamp, safe))
        path.write_text(raw_text, encoding="utf-8")
        return str(path)
    except Exception:
        return None


def _ago(secs):
    if secs >= 3600:
        return "%dh" % (secs // 3600)
    if secs >= 60:
        return "%dm" % (secs // 60)
    return "%ds" % secs


_CACHE_MAX_RAW = 200000  # don't persist outputs bigger than ~200KB


def _small_diff(old, new, max_lines=12):
    """Line diff between two outputs; None when it isn't small enough to be
    worth showing instead of the full compact output."""
    import difflib
    out = []
    for l in difflib.unified_diff(old.split("\n"), new.split("\n"),
                                  lineterm="", n=0):
        if l.startswith(("---", "+++", "@@")):
            continue
        out.append(clip_line(l))
        if len(out) > max_lines:
            return None
    return "\n".join(out) if out else None


def cache_check(argv, raw, ttl=1800, allow_diff=False):
    """Opt-in session cache for read-only commands. If this exact command ran
    recently: identical output -> one 'unchanged since ...' line; slightly
    different output (and allow_diff) -> only the changed lines. The command
    still runs -- only the display is shortened."""
    try:
        key = " ".join(argv)
        h = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
        raw_hash = hashlib.sha1((raw or "").encode("utf-8")).hexdigest()
        p = TT_HOME / "cache" / (h + ".json")
        now = int(time.time())
        note = None
        if p.exists():
            prev = json.loads(p.read_text(encoding="utf-8"))
            fresh = now - prev.get("t", 0) < ttl
            if fresh and prev.get("raw_hash") == raw_hash:
                return "(unchanged since %s ago - tt session cache; " \
                       "use `tt --raw` for full output)" % _ago(now - prev["t"])
            if fresh and allow_diff and prev.get("raw") is not None:
                diff = _small_diff(prev["raw"], raw or "")
                if diff:
                    note = "(changed since %s ago - tt session cache; " \
                           "use `tt --raw` for full output)\n%s" % (
                               _ago(now - prev["t"]), redact_secrets(diff))
        p.parent.mkdir(parents=True, exist_ok=True)
        entry = {"t": now, "raw_hash": raw_hash}
        if len(raw or "") <= _CACHE_MAX_RAW:
            entry["raw"] = raw or ""
        p.write_text(json.dumps(entry), encoding="utf-8")
        return note
    except Exception:
        pass
    return None


def record_stats(label, raw_text, compact_text):
    try:
        TT_HOME.mkdir(parents=True, exist_ok=True)
        entry = {
            "t": int(time.time()),
            "cmd": label,
            "raw": est_tokens(raw_text),
            "out": est_tokens(compact_text),
        }
        with io.open(STATS_FILE, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Filters -- each returns a Result                                            #
# --------------------------------------------------------------------------- #
def _first_path_arg(args, default="."):
    for a in args:
        if not a.startswith("-"):
            return a
    return default


def filter_ls(argv, opts):
    """Compact directory summary instead of verbose `ls -la`."""
    target = _first_path_arg(argv[1:], ".")
    raw_code, raw_text = capture(["ls", "-la", target]) if os.name != "nt" \
        else (0, "")
    try:
        entries = sorted(os.scandir(target), key=lambda e: (not e.is_dir(), e.name))
    except Exception as exc:
        return Result(1, "%s: %s" % (target, exc), raw_text, "ls")
    dirs, files = [], []
    for e in entries:
        if e.name.startswith(".") and not opts.get("all"):
            continue
        if e.is_dir():
            try:
                n = sum(1 for _ in os.scandir(e.path))
            except Exception:
                n = 0
            dirs.append("%s/  (%d)" % (e.name, n))
        else:
            files.append(e.name)
    lines = ["%s  [%d dirs, %d files]" % (target, len(dirs), len(files))]
    lines += ["  " + d for d in dirs]
    if files:
        shown = files[:20]
        lines.append("  " + "  ".join(shown))
        if len(files) > 20:
            lines.append("  ...(+%d more files)" % (len(files) - 20))
    compact = "\n".join(lines)
    raw = raw_text or "\n".join(e.name for e in entries)
    return Result(0, compact, raw, "ls")


def filter_grep(argv, opts):
    """Group matches by file with counts."""
    tool = argv[0]
    code, raw = capture(_build_grep(argv))
    lines = [l for l in raw.split("\n") if l]
    by_file = {}
    order = []
    plain = []
    for l in lines:
        m = re.match(r"^([^:]+):(\d+):(.*)$", l)
        if m:
            f = m.group(1)
            if f not in by_file:
                by_file[f] = []
                order.append(f)
            by_file[f].append("%s: %s" % (m.group(2), m.group(3).strip()[:160]))
        else:
            plain.append(l)
    if not order:
        compact = truncate_lines(dedup_consecutive("\n".join(plain)))
        return Result(code, compact, raw, tool)
    out = ["%d matches in %d files" % (len(lines), len(order))]
    pre, short = _fold_prefix(order)
    if pre:
        out.append("in %s" % pre)
    names = dict(zip(order, short))
    for f in order:
        hits = by_file[f]
        out.append("%s  (%d)" % (names[f], len(hits)))
        for h in hits[:5]:
            out.append("  " + h)
        if len(hits) > 5:
            out.append("  ...(+%d)" % (len(hits) - 5))
    return Result(code, "\n".join(out), raw, tool)


_GREP_EXCLUDES = ["--exclude-dir=.git", "--exclude-dir=node_modules",
                  "--exclude-dir=.venv", "--exclude-dir=venv",
                  "--exclude-dir=__pycache__", "--exclude-dir=dist",
                  "--exclude-dir=build", "--exclude-dir=.mypy_cache"]


def _has_recursive(argv):
    return any(a in ("-r", "-R", "--recursive") for a in argv) or "rg" in argv[0]


def _build_grep(argv):
    """Recurse + line numbers if needed; skip noise dirs (grep only; rg
    already honours .gitignore)."""
    is_grep = argv[0].startswith("grep") or argv[0] == "egrep"
    out = [argv[0]]
    if not _has_recursive(argv):
        out.append("-rn")
    elif not any(a in ("-n", "--line-number") for a in argv):
        out.append("-n")
    if is_grep:
        out += _GREP_EXCLUDES
    out += argv[1:]
    return out


def filter_find(argv, opts):
    code, raw = capture(argv)
    lines = [l for l in raw.split("\n") if l]
    ext = {}
    for l in lines:
        _, dot, e = l.rpartition(".")
        key = e if dot else "(none)"
        ext[key] = ext.get(key, 0) + 1
    out = ["%d results" % len(lines)]
    if ext:
        out.append("by type: " + ", ".join(
            "%s=%d" % (k, v) for k, v in sorted(ext.items(), key=lambda x: -x[1])[:8]
        ))
    shown = lines[:25]
    pre, short = _fold_prefix(shown)
    if pre:
        out.append("in %s" % pre)
        shown = short
    out += shown
    if len(lines) > 25:
        out.append("...(+%d more)" % (len(lines) - 25))
    return Result(code, "\n".join(out), raw, "find")


def filter_git(argv, opts):
    sub = argv[1] if len(argv) > 1 else ""
    if sub == "status":
        # Baseline = what the agent would otherwise pay for (human output).
        base_code, baseline = capture(["git", "status"])
        code, raw = capture(["git", "-c", "color.ui=false", "status",
                             "--porcelain=v1", "--branch"])
        counts = {"M": 0, "A": 0, "D": 0, "R": 0, "?": 0}
        detail = []
        branch = ""
        for l in raw.split("\n"):
            if l.startswith("##"):
                branch = l[2:].strip()
                continue
            if not l.strip():
                continue
            x = l[:2]
            if "?" in x:
                counts["?"] += 1
            elif "M" in x:
                counts["M"] += 1
            elif "A" in x:
                counts["A"] += 1
            elif "D" in x:
                counts["D"] += 1
            elif "R" in x:
                counts["R"] += 1
            detail.append(l)
        summary = "branch %s | %dM %dA %dD %dR %d untracked" % (
            branch or "?", counts["M"], counts["A"], counts["D"],
            counts["R"], counts["?"])
        body = "\n".join(detail[:MAX_LINES])
        compact = summary + ("\n" + body if body else "")
        return Result(code, compact, baseline or raw, "git status")

    if sub == "diff":
        code, raw = capture(["git", "--no-pager", "-c", "color.ui=false",
                             "diff"] + argv[2:])
        return Result(code, _compress_diff(raw), raw, "git diff")

    if sub == "log":
        args = argv[2:]
        # Baseline = the full `git log` the agent would otherwise read.
        base_code, baseline = capture(["git", "--no-pager", "log"] + args)
        oneline_args = list(args)
        if "--oneline" not in oneline_args:
            oneline_args = ["--oneline"] + oneline_args
        if not any(a.startswith("-n") or a.lstrip("-").isdigit()
                   for a in oneline_args):
            oneline_args += ["-n", "30"]
        code, raw = capture(["git", "--no-pager", "log"] + oneline_args)
        return Result(code, truncate_lines(raw), baseline or raw, "git log")

    if sub in ("add", "stage"):
        code, raw = capture(argv)
        return Result(code, "ok" if code == 0 else raw, raw, "git add")

    if sub == "commit":
        code, raw = capture(argv)
        if code == 0:
            _, sha = capture(["git", "rev-parse", "--short", "HEAD"])
            return Result(code, "ok %s" % sha.strip(), raw, "git commit")
        return Result(code, error_lines_only(raw) or raw, raw, "git commit")

    if sub == "push":
        code, raw = capture(argv)
        if code == 0:
            _, br = capture(["git", "rev-parse", "--abbrev-ref", "HEAD"])
            return Result(code, "ok %s" % br.strip(), raw, "git push")
        return Result(code, raw, raw, "git push")

    if sub == "pull":
        code, raw = capture(argv)
        summary = ""
        m = re.search(r"(\d+) files? changed.*", raw)
        if m:
            summary = m.group(0)
        return Result(code, ("ok " + summary).strip() if code == 0 else raw,
                      raw, "git pull")

    if sub == "show":
        code, raw = capture(["git", "--no-pager", "-c", "color.ui=false",
                             "show"] + argv[2:])
        return Result(code, _compress_show(raw), raw, "git show")

    if sub in ("branch", "remote", "tag"):
        code, raw = capture(["git", "--no-pager"] + argv[1:])
        return Result(code, truncate_lines(raw, 40), raw, "git " + sub)

    if sub == "stash" and (len(argv) < 3 or argv[2] == "list"):
        code, raw = capture(["git", "--no-pager", "stash", "list"])
        return Result(code, truncate_lines(raw, 30), raw, "git stash list")

    # Any other git subcommand: run + light compaction.
    code, raw = capture(["git", "--no-pager"] + argv[1:])
    return Result(code, truncate_lines(dedup_consecutive(raw)), raw, "git " + sub)


def _compress_show(raw):
    """git show: keep the commit header, compress the diff body."""
    idx = raw.find("\ndiff --git")
    if idx == -1:
        return truncate_lines(raw, 30)
    header = [h for h in raw[:idx].strip().split("\n") if h.strip()][:8]
    return "\n".join(header) + "\n" + _compress_diff(raw[idx:])


def _compress_diff(raw):
    """Keep a per-file +/- stat and the changed lines; drop context noise."""
    files = []
    current = None
    changed = []
    add = rem = 0
    for l in raw.split("\n"):
        if l.startswith("diff --git"):
            if current:
                files.append((current, add, rem, changed))
            m = re.search(r" b/(.+)$", l)
            current = m.group(1) if m else l
            changed = []
            add = rem = 0
        elif l.startswith("+++") or l.startswith("---") or l.startswith("index ") \
                or l.startswith("new file") or l.startswith("deleted file") \
                or l.startswith("similarity") or l.startswith("rename "):
            continue
        elif l.startswith("@@"):
            # Keep only the '@@ -a,b +c,d @@' range, drop trailing context.
            m2 = re.match(r"^(@@ [^@]*@@)", l)
            changed.append(m2.group(1) if m2 else l)
        elif l.startswith("+"):
            add += 1
            changed.append(l)
        elif l.startswith("-"):
            rem += 1
            changed.append(l)
        # unchanged context lines (start with space) are dropped
    if current:
        files.append((current, add, rem, changed))
    if not files:
        return "no changes"
    out = ["%d file(s) changed" % len(files)]
    for name, a, r, lines in files:
        out.append("%s  (+%d -%d)" % (name, a, r))
        for cl in lines[:40]:
            out.append("  " + clip_line(cl))
        if len(lines) > 40:
            out.append("  ...(+%d changed lines)" % (len(lines) - 40))
    return "\n".join(out)


def filter_test_generic(argv, opts, label=None, cmd=None):
    """Run a test/build command: on success a one-line summary, on failure the
    failing lines only (+ full log saved to disk)."""
    real = cmd if cmd is not None else argv
    label = label or real[0]
    code, raw = capture(real)
    raw = strip_progress(raw)
    if code == 0:
        m = re.search(
            r"(\d+\s+passed[^\n]*|\d+\s+tests?\s+passed[^\n]*|"
            r"ok\b[^\n]*|test result:\s*ok[^\n]*|PASS[^\n]*|"
            r"Tests:.*passed[^\n]*)", raw, re.IGNORECASE)
        summary = m.group(1).strip(" =\t-").strip() if m else ""
        return Result(code, ("PASSED  (%s)" % summary) if summary else "PASSED",
                      raw, label)
    errs = error_lines_only(raw, context=1)
    body = errs or truncate_lines(raw, 30)
    tee = tee_save(real, raw)
    parts = ["FAILED (exit %d)" % code, body]
    if tee:
        parts.append("[full output: %s]" % tee)
    return Result(code, "\n".join(p for p in parts if p), raw, label)


def _compress_pytest(raw):
    """Parse pytest output into: final summary + one block per failing test
    (its `E` assertion lines + location). Returns None if not pytest-shaped."""
    lines = raw.split("\n")
    blocks = []
    cur = None
    in_fail = False
    short = []
    summary = ""
    for l in lines:
        s = l.strip()
        m_sec = re.match(r"^=+\s*(.*?)\s*=+$", s) if s.startswith("=") else None
        if m_sec:
            sec = m_sec.group(1)
            in_fail = sec in ("FAILURES", "ERRORS")
            if re.search(r"(failed|passed|error|skipped)", sec, re.I) \
                    and " in " in sec:
                summary = sec
            if not in_fail:
                cur = None
            continue
        if s.startswith(("FAILED ", "ERROR ")):
            short.append(clip_line(s))
            continue
        if in_fail:
            mh = re.match(r"^_{3,}\s*(.+?)\s*_{3,}$", s)
            if mh:
                cur = (mh.group(1), [])
                blocks.append(cur)
                continue
            if cur is not None and len(cur[1]) < 6 \
                    and (l.startswith("E ") or l.startswith("E\t")
                         or re.match(r"^\S+:\d+:", s)):
                cur[1].append(s)
    if not summary:
        # pytest -q prints the final summary without the ===== decoration.
        for l in reversed(lines):
            s = l.strip()
            if re.match(r"^\d+\s+(failed|passed|error)", s) and " in " in s:
                summary = s
                break
    if not (blocks or short or summary):
        return None
    out = [summary] if summary else []
    used = set()
    for sline in short[:15]:
        out.append(sline)
        for i, (name, det) in enumerate(blocks):
            base = name.split("[")[0].split(".")[-1]
            if i not in used and base and base in sline:
                used.add(i)
                out.extend("  " + d for d in det)
                break
    for i, (name, det) in enumerate(blocks[:15]):
        if i not in used:
            out.append("FAILED " + name)
            out.extend("  " + d for d in det)
    if len(short) > 15:
        out.append("...(+%d more failures)" % (len(short) - 15))
    return "\n".join(out)


def filter_pytest(argv, opts):
    """pytest: one line on success; per-test assertion details on failure."""
    code, raw = capture(argv)
    raw2 = strip_progress(strip_ansi(raw))
    if code == 0:
        m = re.search(r"=+\s*([^=\n]*passed[^=\n]*?)\s*=+", raw2)
        return Result(code, ("PASSED  (%s)" % m.group(1).strip()) if m
                      else "PASSED", raw, "pytest")
    body = _compress_pytest(raw2) or error_lines_only(raw2, 1) \
        or truncate_lines(raw2, 30)
    tee = tee_save(argv, raw)
    parts = ["FAILED (exit %d)" % code, body]
    if tee:
        parts.append("[full output: %s]" % tee)
    return Result(code, "\n".join(parts), raw, "pytest")


def filter_err(argv, opts):
    """tt err <cmd> -- run any command, show only error-ish lines."""
    real = argv[1:]
    if not real:
        return Result(2, "usage: tt err <command...>", "", "err")
    code, raw = capture(real)
    body = error_lines_only(raw, context=1) or "(no error lines detected)"
    tee = tee_save(real, raw) if code != 0 else None
    if tee:
        body += "\n[full output: %s]" % tee
    return Result(code, body, raw, "err " + real[0])


def filter_json(argv, opts):
    """tt json <file|-> -- show JSON structure without bulky/secret values."""
    src = argv[1] if len(argv) > 1 else "-"
    try:
        data = sys.stdin.read() if src == "-" else Path(src).read_text(encoding="utf-8")
        obj = json.loads(_strip_bom(data).strip())
    except Exception as exc:
        return Result(1, "tokentrim json: %s" % exc, "", "json")
    skel = json_skeleton(obj, redact=True)
    compact = json.dumps(skel, separators=(",", ":"), ensure_ascii=False)
    return Result(0, truncate_lines(compact, 80), data, "json", redacted=True)


def _looks_tabular(text):
    lines = [l for l in text.split("\n") if l.strip()]
    if len(lines) < 3:
        return False
    return len(re.split(r"\s{2,}", lines[0].strip())) >= 2


def _vm(size):
    return str(size).replace("Standard_", "") if size else "?"


def _secs(n):
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "?"
    h, rem = divmod(n, 3600)
    m, s = divmod(rem, 60)
    if h:
        return "%dh%dm" % (h, m)
    if m:
        return "%dm%02ds" % (m, s)
    return "%ds" % s


def _az_aks(obj):
    name = obj["name"]
    loc = obj.get("location", "?")
    ver = obj.get("kubernetesVersion", "?")
    prov = obj.get("provisioningState", "?")
    power = (obj.get("powerState") or {}).get("code", "?")
    pools = []
    for p in obj.get("agentPoolProfiles") or []:
        pools.append("%s(%sx%s,%s,%s)" % (
            p.get("name", "?"), p.get("count", "?"), _vm(p.get("vmSize")),
            p.get("osType", "?"), p.get("mode", "?")))
    ident = (obj.get("identity") or {}).get("type", "?")
    aad = "AAD managed" if (obj.get("aadProfile") or {}).get("managed") else \
        "no AAD"
    plugin = (obj.get("networkProfile") or {}).get("networkPlugin", "?")
    tags = obj.get("tags") or {}
    out = ["AKS %s | %s | k8s %s | %s/%s" % (name, loc, ver, prov, power)]
    if pools:
        out.append("Pools: " + ", ".join(pools))
    out.append("Identity: %s | %s | networkPlugin: %s" % (ident, aad, plugin))
    if tags:
        out.append("tags: " + ", ".join("%s=%s" % (k, v)
                                        for k, v in list(tags.items())[:8]))
    return "\n".join(out)


def _aws_sagemaker_training(obj):
    name = obj["TrainingJobName"]
    status = obj.get("TrainingJobStatus", "?")
    billable = _secs(obj.get("BillableTimeInSeconds"))
    itype = (obj.get("ResourceConfig") or {}).get("InstanceType", "?")
    hp = obj.get("HyperParameters") or {}
    hp_str = " ".join("%s=%s" % (k, v) for k, v in list(hp.items())[:6])
    metrics = []
    for m in obj.get("FinalMetricDataList") or []:
        metrics.append("%s=%s" % (m.get("MetricName", "?"),
                                  _fmt_num(m.get("Value"))))
    out = ["%s | %s | %s billable | %s" % (name, status, billable, itype)]
    if hp_str:
        out.append("HP: " + hp_str)
    if metrics:
        out.append("Metrics: " + " ".join(metrics[:8]))
    return "\n".join(out)


def _az_ml_job(obj):
    name = obj.get("display_name") or obj.get("name", "?")
    status = obj.get("status", "?")
    compute = obj.get("compute") or (obj.get("resources") or {}).get(
        "instance_type", "?")
    out = ["AzureML job %s | %s | compute: %s" % (name, status, compute)]
    metrics = obj.get("metrics") or {}
    if isinstance(metrics, dict) and metrics:
        out.append("Metrics: " + " ".join(
            "%s=%s" % (k, _fmt_num(v)) for k, v in list(metrics.items())[:8]))
    return "\n".join(out)


def _gcloud_vertex_job(obj):
    name = obj.get("displayName") or obj.get("name", "?")
    state = obj.get("state", "?")
    specs = ((obj.get("jobSpec") or {}).get("workerPoolSpecs") or [{}])
    mtype = (specs[0].get("machineSpec") or {}).get("machineType", "?")
    out = ["Vertex job %s | %s | %s" % (name, state, mtype)]
    return "\n".join(out)


def _az_cost(obj, cfg):
    props = obj.get("properties", obj)
    cols = [c.get("name") for c in props.get("columns", [])]
    rows = props.get("rows", [])
    if not cols or not rows:
        return None
    ci = next((i for i, c in enumerate(cols)
               if c and "cost" in c.lower()), 0)
    gi = next((i for i, c in enumerate(cols) if c and c.lower() in
               ("resourcegroup", "resourcegroupname", "businessunit")), None)
    if gi is None:
        gi = 1 if len(cols) > 1 else 0
    totals = {}
    grand = 0.0
    for r in rows:
        try:
            cost = float(r[ci])
        except (ValueError, IndexError, TypeError):
            continue
        grp = str(r[gi]) if gi < len(r) else "?"
        if not _resource_allowed(grp, cfg):
            continue
        totals[grp] = totals.get(grp, 0.0) + cost
        grand += cost
    if not totals:
        return None
    top = sorted(totals.items(), key=lambda x: -x[1])[:10]
    out = ["Cost total: %.2f (%d groups)" % (grand, len(totals))]
    for g, c in top:
        out.append("  %s: %.2f" % (g, c))
    return "\n".join(out)


def _az_apim(obj):
    items = obj if isinstance(obj, list) else [obj]
    out = ["%d API item(s)" % len(items)]
    for it in items[:40]:
        if not isinstance(it, dict):
            continue
        name = it.get("name") or it.get("displayName", "?")
        path = it.get("path", "")
        state = "published" if it.get("isCurrent") or \
            it.get("state") == "published" else it.get("state", "")
        out.append(("  %s %s %s" % (name, path, state)).rstrip())
    return "\n".join(out)


def _cloud_specialized(argv, obj, cfg):
    """Route az/aws/gcloud JSON to a field-extracting preset by subcommand."""
    a = argv
    try:
        if "aks" in a and "show" in a and isinstance(obj, dict):
            return _az_aks(obj)
        if "sagemaker" in a and "describe-training-job" in a:
            return _aws_sagemaker_training(obj)
        if "ml" in a and "job" in a and "show" in a:
            return _az_ml_job(obj)
        if "custom-jobs" in a and "describe" in a:
            return _gcloud_vertex_job(obj)
        if "costmanagement" in a or "consumption" in a:
            return _az_cost(obj, cfg)
        if "apim" in a and "list" in a:
            return _az_apim(obj)
    except Exception:
        return None
    return None


def filter_cloud_json(argv, opts):
    """az / aws / gcloud: command-aware field extraction when known, else a
    redacted JSON skeleton; table/text -> compacted."""
    cfg = opts.get("_cfg") or load_config()
    code, raw = capture(argv)
    text = strip_ansi(raw).strip()
    if text[:1] in ("{", "["):
        try:
            obj = json.loads(text)
            special = _cloud_specialized(argv, obj, cfg)
            if special:
                return Result(code, special, raw, " ".join(argv[:3]),
                              redacted=True)
            skel = json_skeleton(obj, redact=True)
            compact = json.dumps(skel, separators=(",", ":"), ensure_ascii=False)
            return Result(code, truncate_lines(compact, 80), raw, argv[0],
                          redacted=True)
        except Exception:
            pass
    if _looks_tabular(text):
        return _fail_wrap(code, compact_table(text), raw, argv, argv[0])
    compact = truncate_lines(dedup_consecutive(strip_progress(text)))
    return _fail_wrap(code, compact, raw, argv, argv[0])


def _resource_allowed(addr, cfg):
    """Apply optional resource_include / resource_exclude regexes from config."""
    inc = cfg.get("resource_include") or []
    exc = cfg.get("resource_exclude") or []
    if inc and not any(re.search(p, addr) for p in inc):
        return False
    if any(re.search(p, addr) for p in exc):
        return False
    return True


def _short_val(v):
    if isinstance(v, (dict, list)):
        return "{...}" if isinstance(v, dict) else "[%d]" % len(v)
    s = str(v)
    return s if len(s) <= 40 else s[:37] + "..."


def _tf_changed_attrs(before, after, limit=6):
    before = before or {}
    after = after or {}
    keys = [k for k in after.keys() if before.get(k) != after.get(k)]
    out = []
    for k in keys[:limit]:
        if SECRET_KEY_RE.search(k):
            out.append("%s: <redacted>" % k)
        else:
            out.append("%s: %s->%s" % (k, _short_val(before.get(k)),
                                       _short_val(after.get(k))))
    hidden = len(keys) - min(len(keys), limit)
    if hidden > 0:
        out.append("(+%d more attrs)" % hidden)
    return out


def _tf_resource_changes(text, cfg):
    """Format `terraform show -json` resource_changes[] compactly."""
    obj = json.loads(text)
    changes = obj.get("resource_changes", [])
    adds = chg = destroy = 0
    lines = []
    for rc in changes:
        addr = rc.get("address", "?")
        ch = rc.get("change", {}) or {}
        actions = ch.get("actions", []) or []
        if actions in (["no-op"], ["read"], []):
            continue
        if not _resource_allowed(addr, cfg):
            continue
        if "create" in actions:
            adds += 1
        if "delete" in actions:
            destroy += 1
        if actions == ["update"]:
            chg += 1
        before, after = ch.get("before"), ch.get("after")
        if set(actions) == {"create", "delete"}:
            lines.append("-/+ %s (replace)" % addr)
            for a in _tf_changed_attrs(before, after):
                lines.append("    " + a)
        elif actions == ["update"]:
            lines.append("~ %s" % addr)
            for a in _tf_changed_attrs(before, after):
                lines.append("    " + a)
        elif actions == ["create"]:
            name = (after or {}).get("name") or (after or {}).get("id") or ""
            lines.append(("+ %s %s" % (addr, _short_val(name))).rstrip())
        elif actions == ["delete"]:
            lines.append("- %s" % addr)
        else:
            lines.append("%s %s" % ("/".join(actions), addr))
    header = "Plan: %d add, %d change, %d destroy" % (adds, chg, destroy)
    if not lines:
        return header + "\nNo resource changes."
    return header + "\n" + "\n".join(lines)


def filter_terraform(argv, opts):
    sub = argv[1] if len(argv) > 1 else ""
    cfg = opts.get("_cfg") or load_config()
    label = "terraform " + sub

    # JSON-native path: `terraform show -json [planfile]` is robust across
    # versions; prefer it over parsing human/ANSI text.
    if sub == "show" and "-json" in argv:
        code, raw = capture(argv)
        try:
            return Result(code, _tf_resource_changes(strip_ansi(raw), cfg), raw,
                          "terraform show -json", redacted=True)
        except Exception:
            return _fail_wrap(code, truncate_lines(dedup_consecutive(raw), 45)
                              + "\n[tt: could not parse JSON; showing raw-ish]",
                              raw, argv, label)

    code, raw = capture(argv)
    raw = strip_progress(strip_ansi(raw))
    if sub in ("plan", "apply"):
        # If a plan file was produced (-out=FILE), read it back as JSON.
        outfile = None
        for a in argv:
            if a.startswith("-out=") or a.startswith("--out="):
                outfile = a.split("=", 1)[1]
        if outfile and sub == "plan" and code == 0:
            jcode, jraw = capture(["terraform", "show", "-json", outfile])
            try:
                summary = _tf_resource_changes(strip_ansi(jraw), cfg)
                return Result(code, summary, raw, "terraform plan (json)",
                              redacted=True)
            except Exception:
                pass  # fall through to text parser
        keep = []
        for l in raw.split("\n"):
            s = l.strip()
            if re.match(r"^#\s", s) or re.match(r"^[+~\-/]{1,2}\s*resource", s) \
                    or "Plan:" in s or "Apply complete" in s \
                    or "No changes" in s or "will be" in s \
                    or ERROR_KEYWORDS.search(s):
                keep.append(l)
        body = "\n".join(keep) if keep else truncate_lines(raw, 25)
        note = "" if keep else "\n[tt: text parse -- run `tt --raw` or use " \
                               "`-out=FILE` for exact JSON plan]"
        return _fail_wrap(code, truncate_lines(body, 50) + note, raw, argv, label)
    if sub == "validate":
        if code == 0:
            return Result(code, "valid", raw, label)
        return _fail_wrap(code, error_lines_only(raw, 1) or truncate_lines(raw, 25),
                          raw, argv, label)
    if sub in ("output", "state", "show", "providers"):
        return _fail_wrap(code, truncate_lines(dedup_consecutive(raw), 45),
                          raw, argv, label)
    return _fail_wrap(code, truncate_lines(dedup_consecutive(raw)), raw, argv,
                      label)


def compact_table(text, drop_headers=None, max_rows=45):
    """Compact a whitespace-aligned table (kubectl get / docker ps / az table):
    optionally drop noisy columns, then truncate rows."""
    lines = [l for l in text.split("\n") if l.strip() != ""]
    if len(lines) < 2:
        return truncate_lines(text, max_rows)
    header = lines[0]
    cols = re.split(r"\s{2,}", header.strip())
    if len(cols) < 2:
        return truncate_lines(text, max_rows)
    drop = set((h or "").upper() for h in (drop_headers or []))
    keep_idx = [i for i, h in enumerate(cols) if h.upper() not in drop]
    out = []
    for l in lines:
        # Drop pure separator rows (e.g. az/psql '---- ----').
        if re.match(r"^[\s\-|+=]+$", l):
            continue
        parts = re.split(r"\s{2,}", l.strip())
        if len(parts) == len(cols):
            out.append("  ".join(parts[i] for i in keep_idx if i < len(parts)))
        else:
            out.append(l.strip())
    return truncate_lines("\n".join(out), max_rows)


def dedup_messages(text, strip_re):
    """Dedup consecutive lines that share the same message after stripping a
    volatile prefix (e.g. journald timestamps). Keeps the first full line."""
    lines = text.split("\n")
    out = []
    prev_key = object()
    first = None
    count = 0

    def flush():
        if first is None:
            return
        out.append(first + ("  (x%d)" % count if count > 1 else ""))

    for l in lines:
        key = strip_re.sub("", l)
        if key == prev_key:
            count += 1
        else:
            flush()
            prev_key = key
            first = l
            count = 1
    flush()
    return "\n".join(out)


def _fail_wrap(code, compact, raw, argv, label, redacted=False):
    """On non-zero exit: prefer error lines and attach the full-output path."""
    if code != 0:
        errs = error_lines_only(strip_ansi(raw), 1)
        if errs:
            compact = errs
        tee = tee_save(argv, raw)
        if tee:
            compact = (compact + "\n[full output: %s]" % tee).strip()
    return Result(code, compact, raw, label, redacted=redacted)


def _describe_keyfields(text):
    """kubectl/oc describe: keep top-level 'Key: value' fields + the Events
    section (where failures live); drop the deeply-nested spec noise."""
    out = []
    in_events = False
    for l in text.split("\n"):
        s = l.strip()
        if s.startswith("Events:"):
            in_events = True
        if in_events:
            out.append(l)
            continue
        if re.match(r"^\s{0,2}[A-Z][A-Za-z0-9 /_.-]*:", l) and len(s) < 200:
            out.append(l)
    return truncate_lines("\n".join(out), 50)


_POD_HEALTHY = {"Running", "Completed", "Succeeded"}


def _kubectl_pods_summary(text):
    """kubectl get pods: count by STATUS; collapse healthy pods to one line and
    surface only anomalies (bad status, restarts>0) with detail."""
    lines = [l for l in text.split("\n") if l.strip()]
    if len(lines) < 2:
        return None
    cols = re.split(r"\s{2,}", lines[0].strip())
    idx = {c.upper(): i for i, c in enumerate(cols)}
    if "STATUS" not in idx or "NAME" not in idx:
        return None
    si, ni = idx["STATUS"], idx["NAME"]
    ri, ai, nodei = idx.get("RESTARTS"), idx.get("AGE"), idx.get("NODE")
    status_count = {}
    anomalies = []
    total = 0
    for l in lines[1:]:
        parts = re.split(r"\s{2,}", l.strip())
        if si >= len(parts):
            continue
        total += 1
        status = parts[si]
        status_count[status] = status_count.get(status, 0) + 1
        restarts = 0
        if ri is not None and ri < len(parts):
            m = re.match(r"(\d+)", parts[ri])
            restarts = int(m.group(1)) if m else 0
        if status not in _POD_HEALTHY or restarts > 0:
            name = parts[ni] if ni < len(parts) else "?"
            age = parts[ai] if (ai is not None and ai < len(parts)) else "?"
            det = "! %s  %s  restarts=%d  age=%s" % (name, status, restarts, age)
            if nodei is not None and nodei < len(parts):
                det += "  node=%s" % parts[nodei]
            anomalies.append(det)
    if total == 0:
        return None
    sc = ", ".join("%d %s" % (v, k) for k, v in
                   sorted(status_count.items(), key=lambda x: -x[1]))
    return "\n".join(["%d pods | %s" % (total, sc)] + anomalies[:30])


def filter_container(argv, opts):
    """docker / kubectl / helm / oc / podman: dedup logs, compact tables,
    summarise describe, keep errors."""
    tool = argv[0]
    sub = argv[1] if len(argv) > 1 else ""
    code, raw = capture(argv)
    raw2 = strip_progress(strip_ansi(raw))
    label = (tool + " " + sub).strip()

    if "logs" in argv or sub == "logs":
        return _fail_wrap(code, truncate_lines(smart_dedup(raw2), 45),
                          raw, argv, label)

    if sub == "describe":
        return _fail_wrap(code, _describe_keyfields(raw2), raw, argv, label)

    # kubectl/oc get pods -> anomaly-focused summary
    if tool in ("kubectl", "oc", "k") and sub == "get" \
            and any(p in argv for p in ("pods", "po", "pod")):
        summary = _kubectl_pods_summary(raw2)
        if summary:
            return _fail_wrap(code, summary, raw, argv, label)

    tabular = sub in ("get", "ps", "images", "image", "ls", "list", "services",
                      "pods", "top") or (tool == "helm" and sub in ("list", "ls"))
    if tabular:
        drop = {
            "docker ps": ["CONTAINER ID", "COMMAND", "PORTS"],
            "docker images": ["IMAGE ID"],
        }.get(label, [])
        if "-o" in argv and "wide" in argv:
            drop = drop + ["IP", "NODE", "NOMINATED NODE", "READINESS GATES"]
        return _fail_wrap(code, compact_table(raw2, drop_headers=drop), raw,
                          argv, label)

    return _fail_wrap(code, truncate_lines(dedup_consecutive(raw2), 45),
                      raw, argv, label)


def filter_journal(argv, opts):
    """journalctl: collapse repeated messages (ignoring timestamps) + truncate."""
    code, raw = capture(argv)
    raw2 = strip_ansi(raw)
    compact = truncate_lines(smart_dedup(raw2), 50)
    return _fail_wrap(code, compact, raw, argv, "journalctl")


def filter_systemctl(argv, opts):
    """systemctl: status -> key fields; list-units -> compact table."""
    sub = argv[1] if len(argv) > 1 else ""
    code, raw = capture(argv)
    raw2 = strip_ansi(raw)
    if sub == "status":
        keep = []
        for l in raw2.split("\n"):
            s = l.strip()
            # ● is the bullet systemd prints before the unit name.
            if re.match(r"^(●|\*|Loaded:|Active:|Main PID:|Tasks:|Memory:|"
                        r"CGroup:|[A-Za-z0-9_.@-]+\.(service|socket|timer))", s) \
                    or ERROR_KEYWORDS.search(s):
                keep.append(l)
        body = "\n".join(keep) if keep else truncate_lines(raw2, 25)
        return _fail_wrap(code, truncate_lines(body, 30), raw, argv,
                          "systemctl status")
    return _fail_wrap(code, compact_table(raw2), raw, argv, "systemctl " + sub)


def filter_curl(argv, opts):
    """curl / wget: strip progress bars; HTML -> visible text; binary/base64
    responses suppressed; truncate the body and save the full copy."""
    code, raw = capture(argv)
    raw2 = strip_progress(raw)
    if _looks_binary(raw2):
        tee = tee_save(argv, raw)
        body = "[tt: ~%dKB binary/base64-like response suppressed; " \
               "use `tt --raw`]" % max(1, len(raw2) // 1024)
        if tee:
            body += "\n[full output: %s]" % tee
        return Result(code, body, raw, argv[0])
    note = ""
    if re.search(r"(?i)<!doctype html|<html[\s>]", raw2):
        raw2 = _html_to_text(raw2)
        note = "\n[tt: HTML converted to text; `tt --raw` for markup]"
    body = truncate_lines(raw2, 40)
    if len(raw2) > len(body) or note:
        tee = tee_save(argv, raw)
        if tee:
            note += "\n[full output: %s]" % tee
    return Result(code, body + note, raw, argv[0])


def _compress_build(raw2, code):
    """Summarise build output; mvn/gradle-aware (BUILD SUCCESS / [ERROR])."""
    if code == 0:
        m = re.search(r"(BUILD SUCCESS(?:FUL)?[^\n]*)", raw2)
        if m:
            msg = "ok -- " + m.group(1).strip()
            t = re.search(r"Total time:\s*([^\n]+)", raw2)
            if t:
                msg += "  (%s)" % t.group(1).strip()
            return msg
        tail = [l for l in raw2.split("\n") if l.strip()][-1:]
        return "ok" + ((" -- " + tail[0][:120]) if tail else "")
    merr = [l for l in raw2.split("\n") if l.startswith("[ERROR]")]
    if merr:
        return "FAILED\n" + truncate_lines(dedup_consecutive("\n".join(merr)), 20)
    errs = error_lines_only(raw2, 1) or truncate_lines(raw2, 25)
    return "FAILED\n" + errs


def filter_build(argv, opts):
    """make / mvn / gradle: one-line summary on success, errors on failure."""
    code, raw = capture(argv)
    raw2 = strip_progress(strip_ansi(raw))
    body = _compress_build(raw2, code)
    if code != 0:
        tee = tee_save(argv, raw)
        if tee:
            body += "\n[full output: %s]" % tee
    return Result(code, body, raw, argv[0])


def filter_pkg(argv, opts):
    """npm / pnpm / yarn / pip: install -> ok+counts; list -> compact."""
    tool = argv[0]
    sub = argv[1] if len(argv) > 1 else ""
    code, raw = capture(argv)
    raw2 = strip_progress(raw)
    if sub in ("install", "i", "add", "ci"):
        if code == 0:
            m = re.search(r"(added|installed|Successfully installed)[^\n]*", raw2,
                          re.IGNORECASE)
            return Result(code, "ok" + ((" -- " + m.group(0)) if m else ""),
                          raw, tool + " " + sub)
        errs = error_lines_only(raw2, 1) or truncate_lines(raw2, 20)
        tee = tee_save(argv, raw)
        return Result(code, "FAILED\n" + errs + (("\n[full output: %s]" % tee)
                      if tee else ""), raw, tool + " " + sub)
    return Result(code, truncate_lines(dedup_consecutive(raw2)), raw,
                  tool + " " + sub)


def filter_lint(argv, opts):
    """eslint / tsc / ruff / golangci-lint: group findings by file."""
    tool = argv[0]
    code, raw = capture(argv)
    raw2 = strip_ansi(raw)
    by_file = {}
    order = []
    other = []
    for l in raw2.split("\n"):
        # tsc:  path(line,col): error TSxxxx: msg
        m = re.match(r"^(.*?)[\(:](\d+)[,:](\d+)\)?:?\s*(.*)$", l.strip())
        if m and m.group(1) and not m.group(1).startswith("-"):
            f = m.group(1)
            by_file.setdefault(f, [])
            if f not in order:
                order.append(f)
            by_file[f].append("%s:%s %s" % (m.group(2), m.group(3),
                                            m.group(4)[:140]))
        elif l.strip():
            other.append(l.strip())
    if not order:
        return Result(code, "clean" if code == 0 else truncate_lines(raw2, 25),
                      raw, tool)
    total = sum(len(v) for v in by_file.values())
    out = ["%d issue(s) in %d file(s)" % (total, len(order))]
    pre, short = _fold_prefix(order[:40])
    if pre:
        out.append("in %s" % pre)
    names = dict(zip(order[:40], short))
    for f in order[:40]:
        out.append("%s  (%d)" % (names[f], len(by_file[f])))
        for item in by_file[f][:6]:
            out.append("  " + item)
        if len(by_file[f]) > 6:
            out.append("  ...(+%d)" % (len(by_file[f]) - 6))
    return Result(code, "\n".join(out), raw, tool)


_SIG_RE = re.compile(
    r"^\s*(def |class |async def |function |func |public |private |protected |"
    r"import |from |export |type |interface |struct |impl |trait |module |"
    r"@|#\s*(TODO|FIXME))")


def filter_cat(argv, opts):
    """Read file(s). Default: full content (safe). Ultra/aggressive: signatures
    only (def/class/import lines) -- big savings when the agent just needs the
    shape of a file, not every line."""
    files = [a for a in argv[1:] if not a.startswith("-")]
    aggressive = opts.get("ultra") or "aggressive" in argv[1:]
    code_ext = ("py", "js", "ts", "tsx", "jsx", "go", "rs", "java", "rb",
                "c", "h", "cpp", "cc", "cs", "php", "kt", "swift", "scala")
    parts, raws = [], []
    code = 0
    for f in files:
        try:
            text = Path(f).read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            parts.append("%s: %s" % (f, exc))
            code = 1
            continue
        raws.append(text)
        ext = f.rsplit(".", 1)[-1].lower() if "." in f else ""
        if aggressive and ext in code_ext:
            sig = [l for l in text.split("\n") if _SIG_RE.match(l)]
            header = "# %s  (%d lines -> %d signatures)" % (
                f, text.count("\n") + 1, len(sig))
            parts.append(header + "\n" + "\n".join(sig))
        else:
            parts.append(text)
    raw = "\n".join(raws)
    compact = "\n".join(parts)
    return Result(code, compact, raw, "cat")


def filter_logfile(argv, opts):
    """tt log <file...> -- dedup repeated lines and truncate."""
    files = [a for a in argv[1:] if not a.startswith("-")]
    if not files:
        return Result(2, "usage: tt log <file...>", "", "log")
    raws, parts = [], []
    for f in files:
        try:
            text = Path(f).read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            return Result(1, "%s: %s" % (f, exc), "", "log")
        raws.append(text)
        parts.append(truncate_lines(smart_dedup(strip_progress(text)), 50))
    return Result(0, "\n".join(parts), "\n".join(raws), "log")


_LIB_FRAME_RE = re.compile(
    r"site-packages|dist-packages|node_modules|[/\\]lib[/\\]python\d|"
    r"[/\\]vendor[/\\]|[/\\]gems[/\\]")


def _collapse_lib_frames(lines):
    """Collapse runs of >=2 stack frames inside library code -- the bug is
    almost never there, and each frame costs a full line."""
    out = []
    buf = []

    def flush():
        if len(buf) >= 2:
            out.append("  ... [%d library frames collapsed] ..." % len(buf))
        else:
            out.extend(buf)
        del buf[:]

    for l in lines:
        s = l.strip()
        is_frame = s.startswith('File "') or s.startswith("at ")
        if is_frame and _LIB_FRAME_RE.search(s):
            buf.append(l)
        else:
            flush()
            out.append(l)
    flush()
    return out


def _compress_trace(text):
    """Keep stack-trace frames + the final error line, drop source snippets;
    collapse runs of library-internal frames."""
    keep = []
    for l in text.split("\n"):
        s = l.strip()
        if s.startswith('File "') or s.startswith("Traceback") \
                or s.startswith("at ") or s.startswith("Caused by") \
                or re.match(r"^[A-Za-z_.]+(Error|Exception|Warning)\b", s) \
                or ERROR_KEYWORDS.search(s):
            keep.append(l)
    keep = _collapse_lib_frames(keep)
    return truncate_lines("\n".join(keep) or text, 50)


def _fmt_num(v):
    return ("%.4g" % v) if isinstance(v, float) else str(v)


_EPOCH_RE = re.compile(r"\bEpoch\s+(\d+)\s*/\s*(\d+)", re.IGNORECASE)
_METRIC_RE = re.compile(
    r"([A-Za-z_][A-Za-z0-9_]*)\s*[:=]\s*([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)")
_METRIC_SKIP = {"epoch", "eta", "step", "steps", "batch", "lr"}


def _looks_training(text):
    if len(_EPOCH_RE.findall(text)) >= 3:
        return True
    return len(re.findall(r"(?im)^\s*epoch[\s:=]", text)) >= 3


def _compress_training(text):
    """ML training logs (Keras/TF/PyTorch): summarise metric trajectories,
    best epoch, and plateau / early-stop signal instead of one line per epoch."""
    lines = text.split("\n")
    has_marker = bool(_EPOCH_RE.search(text))
    buckets = []
    cur = {}
    total = 0

    def push():
        if cur:
            buckets.append(dict(cur))

    if has_marker:
        for l in lines:
            m = _EPOCH_RE.search(l)
            if m:
                push()
                cur.clear()
                total = max(total, int(m.group(2)))
            for mm in _METRIC_RE.finditer(l):
                name = mm.group(1).lower()
                if name in _METRIC_SKIP:
                    continue
                try:
                    cur[name] = float(mm.group(2))
                except ValueError:
                    pass
        push()
    else:
        for l in lines:
            metrics = {}
            for mm in _METRIC_RE.finditer(l):
                name = mm.group(1).lower()
                if name in _METRIC_SKIP:
                    continue
                try:
                    metrics[name] = float(mm.group(2))
                except ValueError:
                    pass
            if any("loss" in k for k in metrics):
                buckets.append(metrics)
    buckets = [b for b in buckets if b]
    if len(buckets) < 2:
        return None
    n = total or len(buckets)

    def series(metric):
        vals = [(i, b[metric]) for i, b in enumerate(buckets) if metric in b]
        return vals

    all_metrics = []
    for b in buckets:
        for k in b:
            if k not in all_metrics:
                all_metrics.append(k)
    preferred = [m for m in ("loss", "val_loss", "accuracy", "acc",
                             "val_accuracy", "val_acc") if m in all_metrics]
    rest = [m for m in all_metrics if m not in preferred]
    ranges = []
    for m in (preferred + rest)[:4]:
        s = series(m)
        if s:
            ranges.append("%s %s->%s" % (m, _fmt_num(s[0][1]),
                                         _fmt_num(s[-1][1])))
    # Best epoch + plateau
    best_line = ""
    for metric, better in (("val_loss", min), ("val_accuracy", max),
                           ("val_acc", max), ("accuracy", max), ("acc", max),
                           ("loss", min)):
        s = series(metric)
        if s:
            bi, bv = better(s, key=lambda x: x[1])
            no_improve = len(buckets) - 1 - bi
            plateau = ""
            if no_improve >= max(5, n // 10):
                plateau = " | No improvement last %d epochs (early-stop " \
                          "candidate)" % no_improve
            best_line = "Best: epoch %d %s=%s%s" % (bi + 1, metric,
                                                    _fmt_num(bv), plateau)
            break
    out = "Training %d epochs (%s)" % (n, ", ".join(ranges))
    if best_line:
        out += "\n" + best_line
    return out


def _compress_pandas_info(text):
    """pandas df.info(): collapse to shape + only null columns + dtype summary."""
    rows = None
    m = re.search(r"(\d[\d,]*)\s+entries", text)
    if m:
        rows = int(m.group(1).replace(",", ""))
    mcols = re.search(r"total\s+(\d+)\s+columns", text)
    cols = mcols.group(1) if mcols else "?"
    mem = ""
    mm = re.search(r"memory usage:\s*([\d.]+\+?\s*[KMG]?B)", text)
    if mm:
        mem = ", " + mm.group(1).replace(" ", "")
    nulls = []
    for l in text.split("\n"):
        c = re.match(r"\s*\d+\s+(\S+)\s+(\d[\d,]*)\s+non-null\s+\w+", l)
        if c and rows is not None:
            nn = int(c.group(2).replace(",", ""))
            miss = rows - nn
            if miss > 0:
                mark = "!" if rows and miss > rows * 0.25 else ""
                nulls.append("%s(-%d%s)" % (c.group(1), miss, mark))
    dsum = ""
    md = re.search(r"dtypes:\s*(.+)", text)
    if md:
        parts = []
        for seg in md.group(1).split(","):
            dm = re.match(r"\s*([A-Za-z_]+)[^\(]*\((\d+)\)", seg)
            if dm:
                parts.append("%s%s" % (dm.group(1), dm.group(2)))
        dsum = ", ".join(parts)
    out = ["DF %s rows x %s cols%s" % (rows if rows is not None else "?",
                                       cols, mem)]
    if nulls:
        out.append("Nulls: " + ", ".join(nulls[:30]))
    if dsum:
        out.append("dtypes: " + dsum)
    return "\n".join(out)


def _compress_classification_report(text):
    """sklearn classification_report: one-line summary + worst-recall class."""
    worst = None
    acc = accn = macro = weighted = None
    for l in text.split("\n"):
        s = l.strip()
        parts = s.split()
        if not parts:
            continue
        if parts[0] == "accuracy" and len(parts) >= 2:
            acc, accn = parts[-2], parts[-1]
        elif s.startswith("macro avg") and len(parts) >= 2:
            macro = parts[-2]
        elif s.startswith("weighted avg") and len(parts) >= 2:
            weighted = parts[-2]
        else:
            m = re.match(r"^(\S.*?)\s+([01]\.\d+)\s+([01]\.\d+)\s+([01]\.\d+)"
                         r"\s+(\d+)$", s)
            if m:
                recall = float(m.group(3))
                if worst is None or recall < worst[1]:
                    worst = (m.group(1).strip(), recall, float(m.group(4)))
    seg = []
    if acc:
        seg.append("accuracy %s%s" % (acc, " (n=%s)" % accn if accn else ""))
    if macro:
        seg.append("macro f1 %s" % macro)
    if weighted:
        seg.append("weighted f1 %s" % weighted)
    out = " | ".join(seg)
    if worst:
        out += ("\n" if out else "") + "worst recall: %s recall=%s f1=%s" % (
            worst[0], _fmt_num(worst[1]), _fmt_num(worst[2]))
    return out or None


def _strip_bom(text):
    """Drop a UTF-8 BOM, decoded either properly or as latin-1 mojibake
    (PowerShell pipes often prepend one and it breaks JSON detection)."""
    for bom in ("\ufeff", "\xef\xbb\xbf"):
        if text.startswith(bom):
            return text[len(bom):]
    return text


def auto_compress(text, redact=True):
    """Detect the shape of arbitrary text (pasted/piped) and compress it.
    Returns (compact_text, redacted_bool)."""
    t = _strip_bom(text).strip()
    if not t:
        return "", False
    if _looks_binary(t):
        return ("[tt: ~%dKB of binary/base64-like content suppressed; "
                "use the original file or `tt --raw`]"
                % max(1, len(t) // 1024)), False
    if t[:1] in ("{", "["):
        try:
            obj = json.loads(t)
            skel = json_skeleton(obj, redact=redact)
            return json.dumps(skel, separators=(",", ":"),
                              ensure_ascii=False), True
        except Exception:
            pass
    if "diff --git" in t or re.search(r"(?m)^@@ .*@@", t):
        return _compress_diff(t), False
    if "pandas.core.frame.DataFrame" in t or "Data columns (total" in t:
        return _compress_pandas_info(t), False
    if "precision" in t and "recall" in t and "f1-score" in t:
        r = _compress_classification_report(t)
        if r:
            return r, False
    if _looks_training(t):
        r = _compress_training(t)
        if r:
            return r, False
    if "Traceback (most recent call last)" in t \
            or re.search(r"(?m)^\s+at .+\(", t) \
            or re.search(r"(?m)^\s+File \".+\", line \d+", t):
        compact = _compress_trace(t)
        if redact:
            masked = redact_secrets(compact)
            return masked, masked != compact
        return compact, False
    compact = truncate_lines(
        smart_dedup(strip_progress(strip_ansi(t))), 60)
    if redact:
        masked = redact_secrets(compact)
        return masked, masked != compact
    return compact, False


def filter_trim(argv, opts):
    """tt trim [file|-] : compress arbitrary pasted or piped text."""
    src = argv[1] if len(argv) > 1 else "-"
    try:
        data = sys.stdin.read() if src == "-" \
            else Path(src).read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return Result(1, "tokentrim trim: %s" % exc, "", "trim")
    compact, redacted = auto_compress(data, redact=True)
    return Result(0, compact, data, "trim", redacted=redacted)


def filter_proxy(argv, opts):
    """tt proxy <cmd> -- run, print raw, but still track savings (=0)."""
    real = argv[1:]
    code, raw = capture(real)
    return Result(code, raw, raw, "proxy " + (real[0] if real else ""))


# --------------------------------------------------------------------------- #
# Dispatch table                                                              #
# --------------------------------------------------------------------------- #
def build_dispatch():
    d = {
        "ls": filter_ls,
        "ll": filter_ls,
        "tree": filter_ls,
        "grep": filter_grep,
        "rg": filter_grep,
        "egrep": filter_grep,
        "ag": filter_grep,
        "find": filter_find,
        "fd": filter_find,
        "cat": filter_cat,
        "read": filter_cat,
        "git": filter_git,
        "terraform": filter_terraform,
        "tofu": filter_terraform,
        "docker": filter_container,
        "kubectl": filter_container,
        "k": filter_container,
        "helm": filter_container,
        "oc": filter_container,
        "podman": filter_container,
        "az": filter_cloud_json,
        "aws": filter_cloud_json,
        "gcloud": filter_cloud_json,
        "npm": filter_pkg,
        "pnpm": filter_pkg,
        "yarn": filter_pkg,
        "pip": filter_pkg,
        "pip3": filter_pkg,
        "eslint": filter_lint,
        "tsc": filter_lint,
        "ruff": filter_lint,
        "golangci-lint": filter_lint,
        "mypy": filter_lint,
        "journalctl": filter_journal,
        "systemctl": filter_systemctl,
        "service": filter_systemctl,
        "curl": filter_curl,
        "wget": filter_curl,
        "make": filter_build,
        "gradle": filter_build,
        "mvn": filter_build,
    }
    # pytest gets a structure-aware preset; other runners share the generic one
    d["pytest"] = filter_pytest
    for t in ("jest", "vitest", "mocha", "rspec", "phpunit"):
        d[t] = lambda a, o, _t=t: filter_test_generic(a, o, label=_t)
    return d


DISPATCH = build_dispatch()

# Two-word runners handled specially in main (e.g. "cargo test", "go test").
TWO_WORD_TESTS = {
    ("cargo", "test"), ("cargo", "build"), ("cargo", "clippy"),
    ("go", "test"), ("go", "build"), ("npm", "test"), ("npm", "run"),
    ("pnpm", "test"), ("yarn", "test"), ("dotnet", "test"),
    ("mvn", "test"), ("gradle", "test"),
}


# --------------------------------------------------------------------------- #
# Meta commands                                                               #
# --------------------------------------------------------------------------- #
COPILOT_INSTRUCTIONS = """\
<!-- Managed by TokenTrim (tt). Safe to keep in version control. -->
## Terminal output compression with `tt` (TokenTrim)

To reduce token usage, **prefix heavy terminal commands with `tt`** when you run
them. `tt` compresses noisy command output (60-90% fewer tokens) while keeping
everything you need: it preserves exit codes and, if a command fails, it saves
the full raw output to disk and prints the path.

Prefix these commands with `tt` (e.g. `tt git diff`, `tt terraform plan`):

- Version control: `git status`, `git diff`, `git log`, `git show`, `git branch`,
  `git add/commit/push/pull`
- Tests / build: `pytest`, `jest`, `vitest`, `cargo test`, `go test`, `npm test`,
  `make`, `mvn`, `gradle`
- Containers / K8s: `docker ps`, `docker logs`, `docker images`, `kubectl get`,
  `kubectl logs`, `kubectl describe`, `helm`, `oc`, `podman`
- Cloud / IaC: `terraform plan`, `terraform apply`, `terraform validate`,
  `az ...`, `aws ...`, `gcloud ...`
- Packages / lint: `npm install`, `pnpm install`, `pip list`, `eslint`, `tsc`,
  `ruff`, `mypy`
- System: `journalctl`, `systemctl status`, `curl`
- Files: `ls`, `grep`, `find`, `cat`

You can also compress arbitrary text before quoting it back by piping it through
`tt trim` (auto-detects JSON, diffs, stack traces, ML training logs, pandas
`df.info()`, and sklearn `classification_report`). For an ML training run, use
`tt train <command>` to get the metric curve, best epoch, and early-stop signal.

At the START of a task, run `tt map` once: it prints a compact overview of the
repository (directories + function/class signatures) so you do not need many
exploratory ls/cat/grep calls.

### Batch work into one step (CodeAct)

When a task needs SEVERAL independent operations (read multiple files, search,
then inspect), do them in ONE `tt code` call instead of many separate tool
calls. Fewer turns means the whole conversation is re-sent to the model fewer
times = large token savings (this compounds when MCP servers are loaded).

Inside `tt code` these helpers are preloaded and already return compressed text:
- `glob(pattern)`           - list files (recursive, skips .git/node_modules/...)
- `view(path, sig=False)`   - read a file (sig=True: signatures only)
- `grep(pattern, path=".")` - grouped matches (pure Python, no grep needed)
- `sh("cmd ...")`           - run a shell command, return compressed output
- `run("cmd1","cmd2",...)`  - run several, return combined compressed output

Example - list TODOs and the shape of each affected file in a single turn:
```
tt code -c '
for f in glob("src/**/*.py"):
    hits = grep("TODO", f)
    if hits.strip():
        print(hits)
        print(view(f, sig=True))
'
```

Prefer one `tt code` over many view/grep/read tool calls whenever the steps do
not need the model to reason in between.

Rules:
- It is always safe to prefix with `tt`; unknown commands run unchanged.
- Do NOT prefix interactive commands (editors, REPLs, `git rebase -i`, `ssh`).
- If you need the full untrimmed output, run the command with `tt --raw <cmd>`
  or without the `tt` prefix.
"""


# Instruction-file targets `tt init` can write. The body is the same for
# every agent -- only the location/format changes.
AGENT_TARGETS = ("copilot", "claude", "agents", "cursor")

_CURSOR_FRONTMATTER = ("---\ndescription: TokenTrim terminal output "
                       "compression\nalwaysApply: true\n---\n\n")


def _write_instructions(target, body):
    target.parent.mkdir(parents=True, exist_ok=True)
    existing = target.read_text(encoding="utf-8") if target.exists() else ""
    if "TokenTrim" in existing:
        print("TokenTrim instructions already present: %s" % target)
        return
    joined = (existing.rstrip() + "\n\n" + body) if existing.strip() else body
    target.write_text(joined, encoding="utf-8")
    print("Wrote agent instructions -> %s" % target)


def _install_claude_hook():
    """Merge a PreToolUse hook into .claude/settings.json so Bash commands are
    rewritten through `tt hook claude`. Experimental: needs a Claude Code
    version that honours updatedInput; harmless otherwise (the CLAUDE.md
    instructions still apply)."""
    p = Path(os.getcwd()) / ".claude" / "settings.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except Exception:
        print("tt init: %s exists but is not valid JSON; not touching it" % p)
        return
    hooks = data.setdefault("hooks", {})
    pre = hooks.setdefault("PreToolUse", [])
    for entry in pre:
        if not isinstance(entry, dict):
            continue
        for h in entry.get("hooks", []):
            if "tt hook" in str(h.get("command", "")):
                print("TokenTrim hook already present: %s" % p)
                return
    pre.append({"matcher": "Bash",
                "hooks": [{"type": "command", "command": "tt hook claude"}]})
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print("Installed Claude Code PreToolUse hook -> %s  (experimental)" % p)


def cmd_init(args):
    """tt init [copilot|claude|cursor|agents|all] [--global] [--hook]"""
    is_global = "--global" in args or "-g" in args
    want_hook = "--hook" in args
    targets = []
    for a in args:
        key = a.lstrip("-").lower()
        if key in AGENT_TARGETS:
            targets.append(key)
        elif key == "all":
            targets = list(AGENT_TARGETS)
    if not targets:
        targets = ["copilot"]
    cwd = Path(os.getcwd())
    rc = 0
    for t in targets:
        try:
            if t == "copilot":
                if is_global:
                    # VS Code user-level prompts folder (best-effort).
                    candidates = [
                        HOME / ".config" / "github-copilot" /
                        "copilot-instructions.md",
                        HOME / "AppData" / "Roaming" / "Code" / "User" /
                        "copilot-instructions.md",
                    ]
                    target = candidates[1] if os.name == "nt" else candidates[0]
                else:
                    target = cwd / ".github" / "copilot-instructions.md"
                _write_instructions(target, COPILOT_INSTRUCTIONS)
            elif t == "claude":
                _write_instructions(cwd / "CLAUDE.md", COPILOT_INSTRUCTIONS)
                if want_hook:
                    _install_claude_hook()
            elif t == "agents":
                _write_instructions(cwd / "AGENTS.md", COPILOT_INSTRUCTIONS)
            elif t == "cursor":
                _write_instructions(cwd / ".cursor" / "rules" / "tokentrim.mdc",
                                    _CURSOR_FRONTMATTER + COPILOT_INSTRUCTIONS)
        except Exception as exc:
            print("tokentrim init failed (%s): %s" % (t, exc))
            rc = 1
    if "copilot" in targets:
        print("Reload VS Code (or the Copilot chat) to pick it up.")
    if targets == ["copilot"] and not is_global:
        print("Tip: `tt init claude|cursor|agents|all` covers other AI "
              "agents; `tt shell-init` guarantees it at the shell level.")
    return rc


# Commands worth routing through tt from a shell wrapper. Interactive uses
# (git rebase -i, docker exec -it, plain `git commit`, npm init, ...) are
# detected by _needs_tty() and passed through untouched.
SHELL_WRAP = ("git", "docker", "kubectl", "helm", "oc", "podman", "terraform",
              "tofu", "npm", "pnpm", "yarn", "pip", "pytest", "eslint", "tsc",
              "ruff", "mypy", "az", "aws", "gcloud", "journalctl", "systemctl",
              "make", "mvn", "gradle", "cargo", "go")


def cmd_shell_init(args):
    """Print shell functions/aliases that route commands through tt.
    Unlike instruction files, this guarantees compression: the agent's
    terminal picks the wrappers up with no cooperation from the model."""
    flavor = ""
    for a in args:
        flavor = a.lstrip("-").lower() or flavor
    if flavor in ("powershell", "pwsh", "ps"):
        flavor = "powershell"
    elif flavor in ("bash", "zsh", "sh"):
        flavor = "bash"
    else:
        flavor = "powershell" if os.name == "nt" else "bash"
    out = ["# TokenTrim shell integration -- the commands below run through",
           "# `tt` automatically. Bypass anytime: `tt --raw <cmd>` or the",
           "# full path to the executable."]
    if flavor == "powershell":
        out.append("# Install: add this line to your $PROFILE:")
        out.append("#   tt shell-init | Out-String | Invoke-Expression")
        for c in SHELL_WRAP:
            out.append("function global:%s { & tt %s @args }" % (c, c))
    else:
        out.append("# Install: add this line to ~/.bashrc or ~/.zshrc:")
        out.append('#   eval "$(tt shell-init bash)"')
        out.append("# Bypass also works with:  command git ...")
        # Functions, not aliases: aliases don't expand in non-interactive
        # shells (bash -c ...), which is exactly what AI agents use.
        for c in SHELL_WRAP:
            out.append('%s() { tt %s "$@"; }' % (c, c))
    sys.stdout.write("\n".join(out) + "\n")
    return 0


def cmd_hook(args):
    """`tt hook claude` -- Claude Code PreToolUse hook: reads the hook JSON on
    stdin and, for a simple Bash command tt knows how to compress, asks the
    harness to run it through tt instead (updatedInput). Prints nothing when
    no rewrite applies, so the harness proceeds unchanged."""
    try:
        payload = json.loads(_strip_bom(sys.stdin.read() or "") or "{}")
    except Exception:
        return 0
    if payload.get("tool_name") != "Bash":
        return 0
    cmdline = ((payload.get("tool_input") or {}).get("command") or "").strip()
    first = cmdline.split(" ", 1)[0] if cmdline else ""
    if not first or first == "tt":
        return 0
    if first not in set(SHELL_WRAP) | set(DISPATCH):
        return 0
    if any(ch in cmdline for ch in "|&;<>`$\n"):
        return 0  # only rewrite simple commands, never shell pipelines
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "updatedInput": {"command": "tt " + cmdline}}}))
    return 0


def _clipboard_tools():
    """Return (read_argv, write_argv) for the OS clipboard, or (None, None)."""
    if os.name == "nt":
        return (["powershell", "-noprofile", "-command", "Get-Clipboard"],
                ["clip"])
    if sys.platform == "darwin":
        return (["pbpaste"], ["pbcopy"])
    for rd, wr in (
        (["wl-paste"], ["wl-copy"]),
        (["xclip", "-selection", "clipboard", "-o"],
         ["xclip", "-selection", "clipboard"]),
        (["xsel", "-b", "-o"], ["xsel", "-b", "-i"]),
    ):
        if shutil.which(rd[0]):
            return (rd, wr)
    return (None, None)


def cmd_clip(argv):
    """Read the clipboard, compress it, write it back -- copy, run, paste."""
    rd, wr = _clipboard_tools()
    if not rd:
        print("tokentrim: no clipboard tool found.\n"
              "Pipe the text through trim instead, e.g.:  <paste> | tt trim")
        return 1
    try:
        got = subprocess.run(rd, stdout=subprocess.PIPE,
                             universal_newlines=True)
        data = got.stdout or ""
    except Exception as exc:
        print("tokentrim clip: could not read clipboard (%s)" % exc)
        return 1
    if not data.strip():
        print("Clipboard is empty.")
        return 0
    compact, redacted = auto_compress(data, redact=True)
    try:
        subprocess.run(wr, input=compact, universal_newlines=True)
    except Exception as exc:
        print("tokentrim clip: could not write clipboard (%s)" % exc)
        print(compact)
        return 1
    rt, ct = est_tokens(data), est_tokens(compact)
    record_stats("clip", data, compact)
    print("Clipboard trimmed: ~%d -> ~%d tokens (%.0f%% saved). Paste now." % (
        rt, ct, 100.0 * (rt - ct) / max(1, rt)))
    return 0


# --------------------------------------------------------------------------- #
# CodeAct mode: run many operations in ONE process (one turn) with helpers      #
# that already return TokenTrim-compressed output.                              #
# --------------------------------------------------------------------------- #
_CA_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__",
                 "dist", "build", ".mypy_cache", ".terraform", ".idea"}


def _default_opts():
    return {"raw": False, "ultra": False, "all": False, "verbose": 0}


def _walk_files(root="."):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _CA_SKIP_DIRS]
        for name in filenames:
            yield os.path.join(dirpath, name)


def _capture_shell(cmdline):
    env = os.environ.copy()
    env.setdefault("NO_COLOR", "1")
    try:
        proc = subprocess.run(cmdline, shell=True, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, encoding="utf-8",
                              errors="replace", env=env)
        return proc.returncode, strip_ansi(proc.stdout or "")
    except Exception as exc:
        return 1, "tokentrim: %s" % exc


def _ca_glob(pattern, limit=500):
    import glob as _g
    hits = [p for p in sorted(_g.glob(pattern, recursive=True))
            if not any(part in _CA_SKIP_DIRS for part in p.split(os.sep))]
    return hits[:limit]


def _ca_view(path, sig=False, lines=None):
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return "%s: %s" % (path, exc)
    if sig:
        keep = [l for l in text.split("\n") if _SIG_RE.match(l)]
        return "# %s  (%d lines -> %d signatures)\n%s" % (
            path, text.count("\n") + 1, len(keep), "\n".join(keep))
    if lines:
        return truncate_lines(text, lines)
    return text


def _ca_grep(pattern, path=".", flags=0, per_file=5):
    try:
        rx = re.compile(pattern, flags)
    except Exception as exc:
        return "bad pattern: %s" % exc
    targets = [path] if os.path.isfile(path) else _walk_files(path)
    groups = []
    total = 0
    for f in targets:
        matched = []
        try:
            with io.open(f, encoding="utf-8", errors="replace") as fh:
                for i, line in enumerate(fh, 1):
                    if rx.search(line):
                        matched.append((i, line.rstrip()[:200]))
        except Exception:
            continue
        if matched:
            total += len(matched)
            groups.append((f, matched))
    if not groups:
        return ""
    out = ["%d matches in %d files" % (total, len(groups))]
    pre, short = _fold_prefix([f for f, _ in groups[:60]])
    if pre:
        out.append("in %s" % pre)
    for (f, matched), disp in zip(groups[:60], short):
        out.append("%s  (%d)" % (disp, len(matched)))
        for ln, txt in matched[:per_file]:
            out.append("  %d: %s" % (ln, txt.strip()))
        if len(matched) > per_file:
            out.append("  ...(+%d)" % (len(matched) - per_file))
    return "\n".join(out)


def _ca_sh(cmdline, raw=False):
    """Run a shell command and return TokenTrim-compressed output (or raw)."""
    import shlex
    has_meta = any(ch in cmdline for ch in "|<>;&$`\n")
    argv = []
    if not has_meta:
        try:
            argv = shlex.split(cmdline)
        except Exception:
            argv = []
    if argv:
        if raw:
            _, out = capture(argv)
            return out
        try:
            res = run_filtered(argv, _default_opts(), load_config())
        except Exception:
            res = None
        if res is not None:
            return res.compact
        _, out = capture(argv)
        return truncate_lines(dedup_consecutive(out))
    code, out = _capture_shell(cmdline)
    if raw:
        return out
    return truncate_lines(dedup_consecutive(strip_progress(strip_ansi(out))))


def _ca_run(*cmds):
    parts = []
    for c in cmds:
        parts.append("$ %s\n%s" % (c, _ca_sh(c)))
    return "\n\n".join(parts)


def _codeact_namespace():
    ns = {
        "__name__": "__ttcode__",
        "glob": _ca_glob,
        "view": _ca_view,
        "read": _ca_view,
        "grep": _ca_grep,
        "sh": _ca_sh,
        "run": _ca_run,
        "json": json,
        "re": re,
        "os": os,
    }
    return ns


def cmd_code(argv):
    """Execute a Python snippet with TokenTrim batch primitives preloaded, in a
    single process -- the CodeAct pattern: many operations, one turn."""
    src = None
    if argv and argv[0] == "-c":
        src = argv[1] if len(argv) > 1 else ""
    elif argv:
        try:
            src = Path(argv[0]).read_text(encoding="utf-8")
        except Exception as exc:
            print("tokentrim code: cannot read %s (%s)" % (argv[0], exc))
            return 1
    else:
        src = sys.stdin.read()
    if not src or not src.strip():
        print("usage: tt code -c '<python>'   |   tt code script.py   |   ... | tt code")
        return 1
    ns = _codeact_namespace()
    try:
        exec(compile(src, "<ttcode>", "exec"), ns)
        return 0
    except SystemExit as exc:
        return int(exc.code or 0)
    except Exception:
        import traceback
        lines = traceback.format_exc().rstrip().split("\n")
        # Show only the user's snippet frames + the final error line; hide
        # TokenTrim's own internals.
        frames = []
        take_next = False
        for l in lines:
            if 'File "<ttcode>"' in l:
                frames.append(l.replace('File "<ttcode>"', "your code line"))
                take_next = True
            elif take_next and l.startswith("    "):
                frames.append(l)
                take_next = False
        err = lines[-1] if lines else "error"
        print("\n".join(frames + [err]) if frames else err)
        return 1


def cmd_map(argv):
    """tt map [path] -- one-shot compact repo map (dirs + code signatures),
    the aider 'repo map' idea: the agent reads this once instead of doing
    many exploratory ls/cat/grep turns."""
    root = argv[0] if argv and not argv[0].startswith("-") else "."
    code_ext = (".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java",
                ".rb", ".cs", ".php", ".kt", ".swift", ".scala", ".c", ".h",
                ".cpp")
    by_dir = {}
    nfiles = 0
    for f in _walk_files(root):
        nfiles += 1
        d = os.path.dirname(f) or "."
        by_dir.setdefault(d, []).append(f)
    out = ["repo map: %s  (%d files, %d dirs)" % (root, nfiles, len(by_dir))]
    for d in sorted(by_dir):
        files = by_dir[d]
        out.append("%s/  (%d)" % (d.replace("\\", "/"), len(files)))
        plain = [os.path.basename(f) for f in files
                 if not f.endswith(code_ext)]
        if plain:
            out.append("  " + "  ".join(plain[:12]) +
                       ("  ...(+%d)" % (len(plain) - 12)
                        if len(plain) > 12 else ""))
        for f in files:
            if not f.endswith(code_ext):
                continue
            try:
                text = Path(f).read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            sigs = [l.strip() for l in text.split("\n") if _SIG_RE.match(l)]
            # imports/decorators are noise at map level; keep def/class/etc.
            sigs = [s for s in sigs
                    if not s.startswith(("import ", "from ", "@"))][:10]
            out.append("  %s  (%d lines)" % (os.path.basename(f),
                                             text.count("\n") + 1))
            for s in sigs:
                out.append("    " + s[:120])
    compact = truncate_lines("\n".join(out), 300)
    sys.stdout.write(compact + "\n")
    record_stats("map", compact, compact)
    return 0


def _load_stats():
    rows = []
    try:
        if STATS_FILE.exists():
            for line in STATS_FILE.read_text(encoding="utf-8").split("\n"):
                if line.strip():
                    rows.append(json.loads(line))
    except Exception:
        pass
    return rows


def cmd_gain(args):
    rows = _load_stats()
    if not rows:
        print("No TokenTrim activity recorded yet. Run some commands via `tt`.")
        return 0
    raw = sum(r.get("raw", 0) for r in rows)
    out = sum(r.get("out", 0) for r in rows)
    saved = raw - out
    pct = (saved * 100.0 / raw) if raw else 0.0
    by_cmd = {}
    for r in rows:
        c = r.get("cmd", "?")
        agg = by_cmd.setdefault(c, [0, 0, 0])
        agg[0] += r.get("raw", 0)
        agg[1] += r.get("out", 0)
        agg[2] += 1
    print("TokenTrim savings")
    print("=================")
    print("commands run : %d" % len(rows))
    print("tokens raw   : %d" % raw)
    print("tokens sent  : %d" % out)
    print("tokens saved : %d  (%.0f%%)" % (saved, pct))
    try:
        price = float(load_config().get("price_per_1k") or 0)
    except (TypeError, ValueError):
        price = 0.0
    if price:
        print("est. saved   : $%.2f  (at $%.4g per 1K tokens)"
              % (saved * price / 1000.0, price))
    print("")
    print("%-18s %8s %8s %6s" % ("command", "raw", "sent", "saved"))
    for c, (r, o, n) in sorted(by_cmd.items(), key=lambda x: -(x[1][0] - x[1][1])):
        p = ((r - o) * 100.0 / r) if r else 0.0
        print("%-18s %8d %8d %5.0f%%" % (c[:18], r, o, p))
    return 0


HELP = """\
TokenTrim (tt) v%s -- compress dev-command output to cut LLM token spend.

Usage:
  tt <command> [args]     run a command through TokenTrim (safe for any command)
  tt --raw <command>      bypass compression (passthrough)
  tt -u <command>         ultra-compact mode
  tt err <command>        run anything, show only error lines
  tt test <command>       run any test/build cmd, summary on pass / failures on fail
  tt json <file|->        show JSON structure without bulky or secret values
  tt log <file...>        dedup + truncate a noisy log file
  tt train <command>      run ML training, summarise the curve (best/plateau)
  tt trim [file|-]        compress pasted/piped text (auto-detects JSON/diff/
                          stack trace/training log/pandas/report) - before pasting
  tt clip                 read clipboard, compress, write it back (copy->run->paste)
  tt code -c '<python>'    CodeAct: run many steps in ONE process/turn with
                          preloaded compressed helpers glob/view/grep/sh/run
  tt map [path]           one-shot compact repo map (dirs + code signatures)
  tt --budget N <cmd>     hard cap: output never exceeds ~N tokens
  tt proxy <command>      passthrough but still measure savings
  tt gain                 show accumulated token savings
  tt init [target]        write AI-agent instructions: copilot (default),
                          claude [--hook], cursor, agents, all; --global
  tt shell-init [shell]   print aliases/functions so commands ALWAYS run
                          through tt (add one line to $PROFILE / .bashrc)
  tt help | --version

Compressed commands: git, ls, grep, find, cat, docker, kubectl (get pods
  anomaly-aware), helm, oc, podman, terraform (JSON-native plan), az (aks/
  costmanagement/apim), aws (sagemaker), gcloud, npm, pnpm, yarn, pip, eslint,
  tsc, ruff, mypy, pytest (assertion-aware), jest, vitest, cargo test, go test,
  journalctl, systemctl, curl (HTML->text), make, mvn/gradle, and more.
  Unknown and interactive commands run unchanged.
Auto-detected in `tt trim`: pandas df.info(), sklearn classification_report,
  Keras/PyTorch training logs, stack traces, binary/base64 blobs.
Config: ~/.tokentrim/config.json (global) and .tokentrim.json (per project).
""" % VERSION


# --------------------------------------------------------------------------- #
# Main                                                                        #
# --------------------------------------------------------------------------- #
def parse_globals(argv):
    opts = {"raw": bool(os.environ.get("TT_RAW")),
            "ultra": bool(os.environ.get("TT_ULTRA")),
            "all": False, "verbose": 0, "budget": 0}
    rest = []
    i = 0
    consuming = True
    while i < len(argv):
        a = argv[i]
        if consuming and a in ("--raw",):
            opts["raw"] = True
        elif consuming and a in ("-u", "--ultra"):
            opts["ultra"] = True
        elif consuming and a in ("-v", "--verbose"):
            opts["verbose"] += 1
        elif consuming and a.startswith("--budget"):
            val = ""
            if "=" in a:
                val = a.split("=", 1)[1]
            elif i + 1 < len(argv):
                i += 1
                val = argv[i]
            try:
                opts["budget"] = int(val)
            except ValueError:
                pass
        else:
            consuming = False
            rest.append(a)
        i += 1
    return opts, rest


# Subcommands / flags that need a real terminal (editor, prompt, TTY).
# Capturing their stdio would break them, so tt passes them through.
_INTERACTIVE_SUBS = {
    "git": {"rebase", "mergetool", "difftool", "instaweb"},
    "docker": {"exec", "attach", "login"},
    "podman": {"exec", "attach", "login"},
    "kubectl": {"exec", "attach", "edit", "port-forward", "proxy"},
    "oc": {"exec", "attach", "edit", "rsh"},
    "terraform": {"console", "login"},
    "az": {"login", "interactive"},
    "aws": {"configure"},
    "gcloud": {"init", "auth"},
    "npm": {"init", "login", "adduser"},
    "pnpm": {"init", "login"},
    "yarn": {"init", "login"},
}


def _needs_tty(argv):
    cmd = argv[0]
    sub = argv[1] if len(argv) > 1 else ""
    if sub in _INTERACTIVE_SUBS.get(cmd, ()):
        return True
    if cmd in ("docker", "podman", "kubectl", "oc") and any(
            a in ("-i", "-it", "-ti", "--interactive", "--stdin")
            for a in argv):
        return True
    if cmd == "git":
        if any(a in ("-i", "--interactive", "-p", "--patch")
               for a in argv[1:]):
            return True
        # `git commit` without a message opens the editor.
        if sub == "commit" and not any(
                re.match(r"^-[a-zA-Z]*m", a) or a.startswith("--message")
                or a in ("-F", "--file", "--no-edit") or a.startswith("-C")
                for a in argv[2:]):
            return True
    return False


def run_filtered(rest, opts, cfg):
    cmd = rest[0]
    opts["_cfg"] = cfg  # let filters read resource_include/exclude etc.

    # explicit generic wrappers
    if cmd == "test":
        return filter_test_generic(rest, opts, label="test", cmd=rest[1:])
    if cmd == "train":
        real = rest[1:]
        code, raw = capture(real)
        comp = _compress_training(strip_ansi(raw)) \
            or truncate_lines(dedup_consecutive(strip_progress(raw)), 40)
        return _fail_wrap(code, comp, raw, real or rest, "train")
    if cmd == "err":
        return filter_err(rest, opts)
    if cmd == "json":
        return filter_json(rest, opts)
    if cmd == "summary":
        code, raw = capture(rest[1:])
        return Result(code, truncate_lines(dedup_consecutive(strip_progress(raw))),
                      raw, "summary")
    if cmd == "proxy":
        return filter_proxy(rest, opts)
    if cmd == "log":
        return filter_logfile(rest, opts)
    if cmd == "trim":
        return filter_trim(rest, opts)

    # two-word test/build runners (cargo test, go test, npm test, ...)
    if len(rest) >= 2 and (cmd, rest[1]) in TWO_WORD_TESTS:
        return filter_test_generic(rest, opts, label=cmd + " " + rest[1],
                                   cmd=rest)

    # Interactive commands (editors, prompts, exec -it) must keep their TTY.
    if _needs_tty(rest):
        return None  # signal passthrough

    if cmd in cfg.get("exclude", []):
        return None  # signal passthrough

    filt = DISPATCH.get(cmd)
    if filt is None:
        return None  # passthrough
    return filt(rest, opts)


def main(argv=None):
    # Never crash printing unicode on legacy Windows codepages.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("help", "-h", "--help"):
        sys.stdout.write(HELP)
        return 0
    if argv[0] in ("--version", "version", "-V"):
        print("tt %s" % VERSION)
        return 0
    if argv[0] == "gain":
        return cmd_gain(argv[1:])
    if argv[0] == "init":
        return cmd_init(argv[1:])
    if argv[0] == "clip":
        return cmd_clip(argv[1:])
    if argv[0] in ("code", "do"):
        return cmd_code(argv[1:])
    if argv[0] == "map":
        return cmd_map(argv[1:])
    if argv[0] == "shell-init":
        return cmd_shell_init(argv[1:])
    if argv[0] == "hook":
        return cmd_hook(argv[1:])

    cfg = load_config()
    opts, rest = parse_globals(argv)
    if cfg.get("ultra"):
        opts["ultra"] = True
    if not rest:
        sys.stdout.write(HELP)
        return 0

    # Raw / passthrough: run transparently, no capture, exact behavior.
    if opts["raw"]:
        return stream_passthrough(rest)

    try:
        result = run_filtered(rest, opts, cfg)
    except Exception as exc:
        # A filter must never break the user's command. Fall back to raw.
        if opts["verbose"]:
            sys.stderr.write("tokentrim: filter error (%s); passthrough\n" % exc)
        return stream_passthrough(rest)

    if result is None:
        # No filter for this command -> transparent passthrough.
        return stream_passthrough(rest)

    # Opt-in session cache: identical output -> one "unchanged" line;
    # slightly changed output -> only the changed lines (never for redacted
    # results, whose raw may contain secrets).
    if (cfg.get("session_cache") or os.environ.get("TT_CACHE")) \
            and result.code == 0:
        note = cache_check(rest, result.raw, allow_diff=not result.redacted)
        if note and (note.startswith("(unchanged")
                     or len(note) < len(result.compact or "")):
            result.compact = note
            result.redacted = True  # keep the short note; don't fall back to raw

    # Safety guarantee: TokenTrim must never send MORE than the raw output.
    # If a filter produced something larger (tiny outputs, odd formats), fall
    # back to the raw text so we never make things worse -- EXCEPT when the
    # filter redacted secrets, where we must keep the redacted (compact) form.
    if result.raw and not result.redacted \
            and len(result.compact or "") > len(result.raw):
        result.compact = result.raw

    # Tee on failure for filters that didn't already do it -- but only when
    # the compact output actually dropped something; if compact >= raw the
    # agent already has everything and the note would only add tokens.
    tee_mode = cfg.get("tee_mode", "failures")
    if result.code != 0 and tee_mode != "never" \
            and "[full output:" not in result.compact \
            and len(result.compact or "") < len(result.raw or ""):
        tee = tee_save(rest, result.raw)
        if tee:
            result.compact = (result.compact + "\n[full output: %s]" % tee).strip()
    elif tee_mode == "always":
        tee_save(rest, result.raw)

    # Optional hard token budget (--budget N / TT_BUDGET / config "budget").
    try:
        budget = int(opts.get("budget") or os.environ.get("TT_BUDGET") or
                     cfg.get("budget") or 0)
    except (TypeError, ValueError):
        budget = 0
    if budget and result.compact:
        result.compact = enforce_budget(result.compact, budget)

    out = result.compact if result.compact is not None else ""
    if not out.endswith("\n"):
        out += "\n"
    sys.stdout.write(out)
    record_stats(result.label, result.raw, result.compact or "")
    return result.code


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
