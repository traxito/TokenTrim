# Contributing to TokenTrim

Thanks for considering a contribution. A few things to know before you open
a PR.

## How PRs are handled

- Every PR is reviewed manually by a maintainer before it's merged — nothing
  lands automatically, and nobody but the maintainers can push directly to
  `main`.
- CI must pass (`python -m unittest discover -s tests`) on all supported
  platforms before a PR is even considered for review.
- Small, focused PRs get reviewed faster than large ones. If you're planning
  something big (a new preset, a new subcommand), open an issue first to
  discuss the approach — it saves everyone rework.

## What's especially welcome right now

TokenTrim's core compression logic has solid unit-test coverage, but real
usage against live infrastructure is thin. See [BENCHMARKS.md](BENCHMARKS.md)
for exactly what has and hasn't been exercised. High-value contributions:

- **Field reports and fixes** from running `tt` against real `docker`,
  `kubectl`, `helm`, `az`, `aws`, or `gcloud` — the presets for these are
  built from unit tests with realistic-but-synthetic captures, not live
  output. If something breaks or looks wrong on real infra, that's exactly
  the kind of bug report (or PR) this project needs.
- **macOS verification** — Windows and Linux (WSL 2 Ubuntu) have been
  exercised live (see BENCHMARKS.md), but the macOS-specific paths
  (pbcopy/pbpaste clipboard, zsh) have not been run on a real Mac.
- **New presets** for commands not yet covered, following the pattern of
  existing `filter_*` functions in `tt.py`.
- **Bug reports with a reproduction** — a raw command + its `tt` output that
  looks wrong is more useful than a description.

## Development setup

No dependencies beyond the standard library to run TokenTrim itself. To
develop:

```bash
git clone <your fork>
cd TokenTrim
python -m unittest discover -s tests -v
```

That's it — `tt.py` and its tests only use the Python standard library.

## Before opening a PR

1. Add or update tests in `tests/test_tt.py` for any behavior change. PRs
   that change `tt.py` without a corresponding test are unlikely to be merged
   as-is.
2. Run the full suite locally: `python -m unittest discover -s tests`.
3. Keep the change focused — a bug fix doesn't need an unrelated refactor
   riding along with it.
4. If you touched the agent-instructions text (`COPILOT_INSTRUCTIONS` in
   `tt.py`), also update `templates/copilot-instructions.md` — the file is a
   manual mirror, not auto-generated.

## Safety invariants — please don't break these

These are the guarantees the whole project is built on; a PR that weakens one
needs a very good reason and a test proving the guarantee still holds
elsewhere:

- Exit codes must always be preserved (raw vs `tt`-wrapped).
- On failure, the full raw output must remain recoverable (tee to disk).
- Compressed output must never exceed the size of the raw output.
- Anything already redacted (secrets, tokens) must never be un-redacted by a
  later fallback path.
- Unknown and interactive commands must run unchanged (passthrough).

## Code style

- Standard library only. No new dependencies for `tt.py` or its tests.
- No comments explaining *what* the code does — only *why*, when it's
  non-obvious (a workaround, an invariant, a subtle constraint).
- Match the existing style: `filter_*` functions return a `Result`, helpers
  are small and composable, regexes are named and commented when the pattern
  isn't self-explanatory.

## Reporting security issues

Please don't open a public issue for a security concern (e.g. a redaction
bypass that could leak a secret). Open a private security advisory on GitHub
instead (Security tab → Report a vulnerability), or contact the maintainer
directly.
