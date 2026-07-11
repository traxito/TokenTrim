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

## Linux results (WSL 2 Ubuntu, native ext4 repo)

The same playground was rebuilt natively inside WSL 2 Ubuntu (Python 3.14,
git 2.53, GNU grep 3.12, GNU findutils 4.10) and the benchmark re-run through
the **real Unix launcher** (`~/.local/bin/tt`, installed by `install.py`).
This exercises paths that don't exist on Windows: real GNU `grep`/`find`,
the real `ls -la` baseline, and the bash shell integration.

| Case (real execution on Linux) | Raw tokens | tt tokens | Saved | Exit preserved |
|---|---:|---:|---:|:--:|
| `git log` (30 commits) | 1,307 | 310 | **76.3%** | ✓ |
| `git status` | 109 | 31 | **71.6%** | ✓ |
| `git diff` (3 files) | 464 | 354 | **23.7%** | ✓ |
| `ls -la` | 150 | 33 | **78.0%** | ✓ |
| `grep -rn` (real GNU grep, grouped) | 447 | 242 | **45.9%** | ✓ |
| `find . -name '*.py'` (tiny output) | 24 | 24 | 0.0% (floor) | ✓ |
| `tt -u cat` (signatures only) | 152 | 48 | **68.4%** | ✓ |
| `curl https://example.com` | 140 | 64 | **54.3%** | ✓ |
| 916-line app log (`tt log`) | 16,643 | 800 | **95.2%** | ✓ |
| AWS-style JSON (`tt json`) | 6,073 | 130 | **97.9%** | ✓ |

The full 76-test unit suite also passes on Linux (1 skip: a Windows-only
case). The installer, launcher creation (with the executable bit) and
`tt --version` through `~/.local/bin/tt` were all verified live.

**Linux-only bug found and fixed:** `tt shell-init bash` originally emitted
`alias git='tt git'` — but **aliases don't expand in non-interactive bash**
(`bash -c ...`), which is exactly how AI agents run commands. Verified live on
WSL: with aliases, a plain `git status` bypassed tt entirely; with shell
functions (`git() { tt git "$@"; }`) interception works in both interactive
and non-interactive shells. `shell-init` now emits functions
(bypass: `command git ...`).

## Real infrastructure round: Docker, Kubernetes, AWS, systemd, a real OSS repo

A second round (2026-07-11) went after the presets that hadn't been exercised
live. Everything below ran against **real software, not mocks of tt's
making**: a fresh clone of [pallets/flask](https://github.com/pallets/flask),
a real Docker Engine with running/crashing containers, a real single-node
Kubernetes cluster (k3s v1.36), the real `aws` CLI talking to an
AWS-API-compatible server (moto), and the live systemd journal — all inside
WSL 2 Ubuntu.

| Case | Raw tokens | tt tokens | Saved | Exit |
|---|---:|---:|---:|:--:|
| **flask**: `git log` (full real history) | 324,172 | 283 | **99.9%** | ✓ |
| **flask**: `grep -rn 'def ' src` | 7,661 | 1,317 | **82.8%** | ✓ |
| **flask**: `git show HEAD` | 188 | 89 | **52.7%** | ✓ |
| **flask**: `ls -la` | 241 | 36 | **85.1%** | ✓ |
| `journalctl -n 300` (live journal) | 8,035 | 1,177 | **85.4%** | ✓ |
| `systemctl status` (real unit) | 515 | 108 | **79.0%** | ✓ |
| `docker logs` (400 timestamped errors) | 9,600 | 26 | **99.7%** | ✓ |
| `docker ps` | 55 | 35 | **36.4%** | ✓ |
| `kubectl get pods` (1 CrashLoop of 4) | 74 | 17 | **77.0%** | ✓ |
| `kubectl get pods -A` (11 pods) | 282 | 35 | **87.6%** | ✓ |
| `kubectl describe pod` (broken pod) | 962 | 594 | **38.3%** | ✓ |
| `aws ec2 describe-instances` (6 instances) | 8,493 | 130 | **98.5%** | ✓ |
| `aws ec2 describe-images` (full AMI catalog) | 335,308 | 130 | **~100%** | ✓ |
| `tt clip` (Windows clipboard round-trip) | 3,312 | 31 | **99.1%** | ✓ |

Highlights worth calling out:

- **`git log` on a real repo is a context bomb**: Flask's full history is
  ~324K tokens — more than most models' context windows — and an agent that
  runs it raw pays for all of it. Through tt: 283 tokens.
- **The anomaly-aware kubectl preset works against a real cluster.** With 3
  healthy pods and one crash-looping, `tt kubectl get pods` printed exactly:
  `4 pods | 3 Running, 1 Error` + `! crashy  Error  restarts=2  age=82s`.
- **`docker logs` with real timestamps dedups to one line**: 400 distinct
  ISO-stamped error lines → `... connection refused  (x400)`.
- **Secret redaction held under the real AWS CLI**: `ClientToken` and
  credential-shaped fields came back as `<redacted>` in the compact JSON.
- Exit codes were preserved on every case, including a case where both raw
  and tt-wrapped `kubectl logs` failed identically (exit 1/1) — tt reported
  the failure faithfully instead of masking it.

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

Tested for real: git (including a real OSS repo), pytest, mypy, npm, pip,
terraform (init/plan), **docker** (real Engine, running + crashing
containers), **kubectl** (real k3s cluster), **aws CLI** (against moto, an
AWS-API-compatible server — not AWS itself), journalctl/systemctl (live
systemd), curl, ls, grep, find, cat, log files, JSON, trim, clip (real
Windows clipboard), map, gain, shell-init, both installers and the PATH
setup — on **Windows 11 / PowerShell** and **Linux (WSL 2 Ubuntu) / bash**.

Still not exercised live: helm, oc, podman, the az and gcloud CLIs against
real Azure/GCP subscriptions, aws against actual AWS (moto speaks the same
API but real accounts have bigger/messier payloads), and macOS
(pbcopy/pbpaste, zsh). Field reports and PRs welcome — see the repo issues.

## Reproduce it

```bash
python install.py
tt git status          # any repo
tt pytest              # any test suite
tt log <your-app.log>
tt gain                # your own dashboard
```
