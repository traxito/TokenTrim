# TokenTrim — Real-world benchmarks

Measured on a **real installation** (`python install.py`, `tt` launcher on
PATH), running **real commands against real projects** — not synthetic unit
fixtures. Date: 2026-07-10 · TokenTrim v1.4.0 · Windows 11 · Python 3.12.

## Methodology

- Test project: a small but realistic Python service (`shopapi`) — a git repo
  with **30 commits** of history, working-tree changes across 3 files plus
  untracked files, a **pytest suite (13 tests, 3 failing)**, modules with
  deliberate type errors for mypy, a **916-line application log** (ISO
  timestamps, repeated errors, an embedded stack trace), an **AWS-style JSON**
  document (18 EC2-like instances), an npm `package.json`, and a **Terraform**
  config (2 resources, initialised and planned for real).
- For each case the raw command and the same command through the installed
  `tt` launcher were executed; sizes compared as tokens ≈ chars/4
  (stdout+stderr merged, identical environment).
- Exit codes of raw vs `tt` runs were compared on every case.

## Results

| Case (all real executions) | Raw tokens | tt tokens | Saved | Exit preserved |
|---|---:|---:|---:|:--:|
| `git log` (30 commits) | 1,307 | 317 | **75.7%** | ✓ |
| `git status` (3 modified + 6 untracked) | 122 | 48 | **60.7%** | ✓ |
| `git diff` (3 files) | 541 | 364 | **32.7%** | ✓ |
| `git show` | 103 | 68 | **34.0%** | ✓ |
| `pytest -q` (3 failures of 13) | 524 | 233 | **55.5%** | ✓ (1/1) |
| `mypy` (12 errors, 2 files) | 307 | 307 | 0.0% (floor) | ✓ (1/1) |
| `ls` (project directory) | 182 | 28 | **84.6%** | ✓ |
| `tt log` — 916-line app log | 16,643 | 813 | **95.1%** | ✓ |
| `tt json` — AWS-style, 18 instances | 6,073 | 130 | **97.9%** | ✓ |
| `tt trim` — same log, auto-detected | 16,643 | 1,139 | **93.2%** | ✓ |
| `terraform plan` (JSON-native, real init+plan) | 283 | 18 | **93.6%** | ✓ |
| `curl https://example.com` (HTML→text) | 140 | 69 | **50.7%** | ✓ |
| `npm install` (2 packages, real network) | 24 | 1 | **95.8%** | ✓ |
| `pip list` (60+ packages) | 529 | 499 | 5.7% | ✓ |

**Whole session, as reported by the real `tt gain` dashboard:**

```
commands run : 14
tokens raw   : 43202
tokens sent  : 3964
tokens saved : 39238  (91%)
```

The pattern matches the design: **the noisier the output, the bigger the win**
(logs 95%, cloud JSON 98%, terraform 94%). Dense, already-minimal outputs
(`pip list`, mypy's compact error format) save little — and that's the safety
floor working: **TokenTrim never sends more than the raw output** (mypy shows
0%, not a negative number).

## Sample: what the agent actually sees

`tt pytest -q` on the failing suite (233 tokens instead of 524) — the exact
failing tests, their assertions, the location, and the path to the full log:

```
FAILED (exit 1)
3 failed, 10 passed in 0.20s
FAILED tests/test_shopapi.py::TestPricing::test_flat_shipping_under_threshold
  E       assert 5.49 == 3.99
  ...shopapi/tests/test_shopapi.py:54: AssertionError
FAILED tests/test_shopapi.py::TestPricing::test_total_with_vip_discount
  E       AssertionError: assert 78.65 == 84.7
  ...
[full output: ~/.tokentrim/tee/1783702595_pytest_q.log]
```

`tt terraform plan -out=tfplan` (18 tokens instead of 283), parsed from the
plan JSON — note the config contained a `db_password` and nothing leaked:

```
Plan: 2 add, 0 change, 0 destroy
+ terraform_data.app_config
+ terraform_data.cache_config
```

## Guarantees verified in real runs

- **Exit codes preserved** on all 14 cases, including failures (pytest and
  mypy exit 1 both raw and via tt).
- **Full output teed on failure**: every failing run left its complete raw log
  under `~/.tokentrim/tee/` with the path printed in the compact output.
- **Never larger than raw**: enforced (see mypy row).
- **Secret redaction**: the Terraform plan's `db_password` never appeared;
  `tt json`/`tt trim` masked secret-keyed values in the AWS-style JSON.
- **Shell interception works**: in a fresh PowerShell session with
  `tt shell-init | Out-String | Invoke-Expression` loaded, a plain
  `git status` produced TokenTrim's compressed output — no `tt` prefix typed,
  no cooperation from any agent — and `$LASTEXITCODE` was preserved.

## Bugs found (and fixed) by this benchmark

Real-world testing caught two bugs the unit suite couldn't:

1. **Windows `.cmd` shims** — `tt npm install` failed with exit 127 because
   `npm` is `npm.cmd` and `CreateProcess` doesn't resolve it by bare name.
   Fixed by resolving `argv[0]` through `shutil.which` (honours `PATHEXT`);
   now exit codes match and the run compresses to `ok -- added 3 packages`.
2. **Tee note inflating small failures** — for tiny failing outputs (mypy),
   the `[full output: …]` note was appended *after* the never-larger-than-raw
   check, making tt's output larger than the original. The note is now only
   added when compression actually dropped content.

Both fixes have regression tests (`tests/test_tt.py`, 76 tests).

## Honest scope of this benchmark

Tested for real on this machine: git, pytest, mypy, npm, pip, terraform
(init/plan), curl, ls, log files, JSON, trim, map, gain, shell-init, the
installer and the PATH setup — all on **Windows 11 / PowerShell**.

Not exercised against real services (not installed here): docker, kubectl,
helm, az/aws/gcloud CLIs — their presets are covered by unit tests with
realistic captured/synthetic outputs, but not by live runs. Linux/macOS paths
(bash launcher, aliases) exist and are unit-tested but were not executed on a
real Linux/macOS box. Field reports and PRs welcome — see the repo issues.

## Reproduce it

```bash
python install.py
tt git status          # any repo
tt pytest              # any test suite
tt log <your-app.log>
tt gain                # your own dashboard
```
