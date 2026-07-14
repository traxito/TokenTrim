# TokenTrim (`tt`)

**Cut the token bill of your AI coding assistant by compressing noisy command
output before it ever reaches the model's context with zero loss of the
information the assistant actually needs.**

When your AI assistant (GitHub Copilot, etc.) runs a command like `git diff`,
`terraform plan`, `docker logs` or `pytest`, the entire raw output is pushed into
the conversation and you pay for every token,progress bars, boilerplate,
repeated log lines, unchanged diff context and all. TokenTrim sits in front of
those commands, keeps the signal (errors, changed lines, summaries) and drops
the noise. Typical reduction: **60–90%**.

It is the high-impact idea behind [`rtk`](https://github.com/rtk-ai/rtk),
rebuilt as a **single Python file with no dependencies** so it installs in two
steps and runs **natively on Windows** no Rust, no Homebrew, no WSL, no `uv`,
no plugin marketplace, no sandbox VM.

---

## Install (2 steps)

You only need Python 3.8+ (already on macOS/Linux; on Windows install from
python.org or the Store).

```bash
# 1. Clone the repo: git clone https://github.com/traxito/TokenTrim.git

# 2. Go to the TokenTrim folder: cd TokenTrim

# 3. Install it:
python3 install.py

# 4. reopen your terminal (and reload VS Code) , done.
```

That's it. `install.py`:
- copies the engine to `~/.tokentrim/`,
- creates a `tt` command on your PATH (Windows: prints the one line to add it),
- writes `.github/copilot-instructions.md` into the current repo so **Copilot
  starts using `tt` automatically** (see below).

Options: `python install.py --global` (user-wide Copilot instructions) ·
`--no-copilot` (CLI only) · `--uninstall` (remove everything).

---

## How it saves tokens

For every wrapped command TokenTrim applies four strategies (same as `rtk`):

| Strategy | Example |
|---|---|
| **Filter** | drop `git diff` context lines, index/`+++`/`---` headers |
| **Group** | lint errors grouped by file; grep matches grouped by file |
| **Truncate** | keep head+tail of huge output, note how many lines were cut |
| **Deduplicate** | collapse repeated log lines into `line  (x143)` |

**Measured savings, real installation, real commands, real projects**
(full methodology, sample outputs and caveats in [BENCHMARKS.md](BENCHMARKS.md)):

| Case (real execution) | Raw | TokenTrim | Saved |
|---|---:|---:|---:|
| `git log` on a real OSS repo (Flask, full history) | 324,172 | 283 | **99.9%** |
| `docker logs` (400 timestamped errors, real Engine) | 9,600 | 26 | **99.7%** |
| `aws ec2 describe-instances` (real CLI, 6 instances) | 8,493 | 130 | **98.5%** |
| `kubectl get pods -A` (real k3s cluster, 11 pods) | 282 | 35 | **88%** |
| 916-line app log (`tt log`) | 16,643 | 813 | **95%** |
| `terraform plan` (real init+plan, JSON-native) | 283 | 18 | **94%** |
| `journalctl -n 300` (live systemd journal) | 8,035 | 1,177 | **85%** |
| `grep -rn` over Flask's real source | 7,661 | 1,317 | **83%** |
| `pytest` (3 failures of 13 tests) | 524 | 233 | **56%** |
| `npm install` (real, 2 packages) | 24 | 1 | **96%** |

Whole benchmark session as reported by `tt gain`: **43,202 raw → 3,964 sent
(91% saved)**. The noisier the output, the bigger the win; dense outputs like
`pip list` save little , and can never go negative, because TokenTrim falls
back to the raw text if compressing wouldn't help.

Run the suite yourself (standard library only):

```bash
python -m unittest discover -s tests -v
```

---

## Safety, it never costs you quality

- **Exit codes are always preserved.** The assistant still sees pass/fail correctly.
- **Failures are never truncated away.** On a non-zero exit, the *full* raw
  output is saved to `~/.tokentrim/tee/…` and the path is printed, so the
  assistant can read it without re-running the command.
- **Never larger than raw.** If compressing would somehow produce *more* text,
  TokenTrim falls back to the raw output.
- **Secrets are redacted** in JSON from `aws`/`az`/`tt json` (keys matching
  secret/password/token/access-key… become `<redacted>`), and `tt trim`/`tt clip`
  also mask `key=value` secret assignments and well-known token formats (AWS
  access keys, GitHub/Slack tokens, JWTs). Redaction is never undone by the
  size fallback.
- **Unknown commands run unchanged** (transparent passthrough).
- **Escape hatch:** `tt --raw <cmd>` (or `TT_RAW=1`) bypasses compression entirely.
- A filter that ever errors falls back to running your command normally , `tt`
  will not break a command.

---

## Usage

Prefix any command with `tt` (safe for anything):

```bash
tt git status          tt git diff            tt git log
tt pytest              tt cargo test          tt go test
tt docker ps           tt docker logs web     tt kubectl get pods
tt terraform plan      tt aws ec2 describe-instances
tt npm install         tt eslint .            tt tsc
tt ls                  tt grep -rn TODO .     tt find . -name '*.py'
```

Handy extras:

```bash
tt gain                # dashboard of tokens saved so far (and $ if configured)
tt map                 # one-shot compact repo map: dirs + code signatures
tt err <command>       # run anything, show only the error lines
tt test <command>      # any test/build runner: summary on pass, failures on fail
tt json <file|->       # JSON structure without bulky/secret values
tt log app.log         # dedup + truncate a noisy log file
tt trim <file|->       # compress pasted/piped text (see below)
tt clip                # compress whatever is on your clipboard, in place
tt code -c '<python>'  # CodeAct: many steps in one turn (see below)
tt --budget 300 <cmd>  # hard cap: output can never exceed ~300 tokens
tt -u cat module.py    # ultra mode: show only signatures (def/class/import)
tt shell-init          # shell functions so EVERY command runs through tt
tt --raw <command>     # bypass compression
tt help
```

**Supported commands:** git (status/diff/log/show/branch/…), ls, grep, find,
cat/read, docker, kubectl, helm, oc, podman, terraform/tofu, az, aws, gcloud,
npm, pnpm, yarn, pip, eslint, tsc, ruff, mypy, golangci-lint, pytest, jest,
vitest, cargo test, go test, journalctl, systemctl, curl, wget, make, mvn,
gradle, and more. Anything else runs unchanged.

---

## Guaranteed adoption, shell integration & agent hooks

Instruction files ask the agent to use `tt`; these two mechanisms don't ask.

**Shell wrappers (`tt shell-init`).** Prints shell functions that route
`git`, `docker`, `kubectl`, `terraform`, `npm`, `pytest`, `az`, `aws`… through
`tt` in *every* terminal , including the one your AI agent uses , with zero
cooperation from the model. One line to install:

```powershell
# PowerShell: add to $PROFILE
tt shell-init | Out-String | Invoke-Expression
```

```bash
# bash/zsh: add to ~/.bashrc or ~/.zshrc
eval "$(tt shell-init bash)"
```

Interactive uses are auto-detected and passed through untouched: `git rebase -i`,
plain `git commit` (opens your editor), `docker exec -it`, `kubectl edit`,
`az login`, `npm init`… Bypass anytime with `tt --raw <cmd>`, `command git …`
(bash) or the executable's full path.

**Other AI agents (`tt init <target>`).** The same always-on instructions can
be written for:

```bash
tt init            # .github/copilot-instructions.md (default, VS Code Copilot)
tt init claude     # CLAUDE.md (Claude Code)
tt init cursor     # .cursor/rules/tokentrim.mdc (Cursor)
tt init agents     # AGENTS.md (emerging cross-agent standard)
tt init all        # all of the above
```

**Claude Code hard hook (experimental).** `tt init claude --hook` also installs
a `PreToolUse` hook in `.claude/settings.json` that rewrites simple Bash
commands (`git status` → `tt git status`) *before* they run , real interception,
not an instruction. Pipelines and unknown commands are never touched; on Claude
Code versions without `updatedInput` support it is a harmless no-op.

---

## GitHub Copilot in VS Code, automatic use

The installer writes **`.github/copilot-instructions.md`**, VS Code's documented
"always-on" instructions file. It tells Copilot's agent to prefix heavy terminal
commands with `tt`, so compression happens **without you typing the prefix**.
Reload VS Code (or the Copilot chat) after installing.

Re-generate it anytime from a repo with `tt init` (or `tt init --global` for a
user-wide file). The template is in `templates/copilot-instructions.md`.

> Note: VS Code Copilot's public customisation surface is instruction files, not
> hard command-interception hooks. So the agent adopts `tt` because it is
> instructed to , very reliable in practice, and you can always run `tt`
> yourself in the terminal for guaranteed compression.

---

## Pasting a lot of text into the chat

A common token sink is **pasting** a huge log, JSON blob or stack trace straight
into the chat. TokenTrim can shrink that first.

Honest limitation: VS Code Copilot does **not** expose a hook to rewrite *your*
chat message before it's sent, so nothing can transparently trim the chat box.
What TokenTrim gives you instead is a one-step workflow:

- **Clipboard round-trip (recommended):** copy the text, run `tt clip`, then
  paste , the clipboard now holds the compressed version.

  ```bash
  # after copying a 700-line log:
  tt clip
  # -> Clipboard trimmed: ~9000 -> ~600 tokens (93% saved). Paste now.
  ```

- **Pipe / file:** `some-command | tt trim`, or `tt trim big.log`, then copy the
  result.

`tt trim`/`tt clip` auto-detect the content, JSON (structure only, secrets
redacted), unified diffs, stack traces (frames + error kept), or generic logs
(dedup + truncate).

Tip: bind `tt clip` to a VS Code task or an OS hotkey to make it a single
keystroke before pasting.

---

## CodeAct mode, many steps in one turn

The other big token drain isn't output size, it's **turns**. Every time the
assistant makes a tool call, waits, and calls again, the entire conversation
(system prompt, tool definitions, prior messages) is re-sent to the model. Five
`view`/`grep` calls = five replays. With MCP servers loaded, each replay is huge.

`tt code` collapses that into **one** call. It runs a Python snippet in a single
process, with helpers preloaded that already return compressed output:

| Helper | Does |
|---|---|
| `glob(pattern)` | list files recursively (skips `.git`, `node_modules`, …) |
| `view(path, sig=False)` | read a file; `sig=True` → only signatures |
| `grep(pattern, path=".")` | grouped matches, pure Python (no `grep` needed) |
| `sh("cmd …")` | run a shell command, return **compressed** output |
| `run("c1","c2",…)` | run several, return combined compressed output |

```bash
# find every TODO and show the shape of each affected file , in ONE turn:
tt code -c '
for f in glob("src/**/*.py"):
    hits = grep("TODO", f)
    if hits.strip():
        print(hits)
        print(view(f, sig=True))
'
```

The installer adds a CodeAct section to `.github/copilot-instructions.md`, so
Copilot's agent prefers one `tt code` over many separate tool calls. This is the
[`copilot-codeact`](https://github.com/jsturtevant/copilot-codeact-plugin) idea,
but with no `uv`, no marketplace and no sandbox VM , just Python.

> Trust: `tt code` runs the snippet on your machine with normal privileges ,
> exactly like the assistant running a shell script. Only the assistant (which
> you already trust to run commands) writes these snippets.

---

## Specialised presets (Cloud / IaC / ML)

Beyond generic compression, some commands get **structure-aware** presets that
extract only what matters:

- **Terraform (JSON-native).** `tt terraform plan -out=FILE` (or
  `tt terraform show -json FILE`) parses `resource_changes[]` instead of fragile
  text: `Plan: N add, M change, K destroy`, then per resource only the
  **changed** attributes (`before->after`), secrets redacted. Plain
  `terraform plan` still works via the text parser (with a note that JSON is
  more exact).
- **`az aks show`** → `AKS <name> | <region> | k8s <ver> | <state>` + node pools
  + identity/network + tags (drops fqdn/servicePrincipal/long arrays).
- **`kubectl get pods`** → counts by status; when all healthy, one line
  (`12 pods | 12 Running`); when not, only the anomalies with detail
  (`! app-x CrashLoopBackOff restarts=7 age=45m node=...`).
- **`aws sagemaker describe-training-job`** → name, status, billable time,
  instance, hyper-params, final metrics. Same pattern for **`az ml job show`**
  and **`gcloud ai custom-jobs describe`**.
- **`az costmanagement query`** → grouped by resource group / BU, summed, top-N,
  with total. Honours `resource_include`/`resource_exclude`.
- **`az apim api/product list`** → name, path, state per item.
- **`pytest`** → on failure, only the final summary + each failing test's
  assertion (`E`) lines and location; captured stdout and source snippets are
  dropped. On success, one line.
- **`mvn` / `gradle`** → `ok -- BUILD SUCCESS (12.3 s)` on success; only the
  `[ERROR]` lines on failure (the `[INFO]` firehose is dropped).
- **`curl` / `wget`** → HTML responses are converted to visible text (tags,
  scripts and styles stripped); binary/base64 responses are suppressed with a
  note (the full copy is teed to disk either way).
- **Logs anywhere** (docker logs, journalctl, `tt log`, `tt trim`): repeated
  lines are collapsed even when each has a different timestamp (ISO/syslog/
  apache), including interleaved A-B-A-B patterns → `line  (x143 total)`.
- **Stack traces** → runs of frames inside `site-packages`/`node_modules` are
  collapsed to `... [12 library frames collapsed] ...`; your code's frames and
  the exception always survive.

**Data science / ML , paste or wrap:**

- **Training logs** (Keras / TF / PyTorch): `tt train python train.py`, or paste
  through `tt trim`. Instead of one line per epoch you get the metric
  trajectories, the **best epoch**, and a **plateau / early-stop** signal ,
  e.g. `Best: epoch 41 val_loss=0.0522 | No improvement last 19 epochs`. ~99%.
- **`pandas` `df.info()`** → `DF 184320 rows x 22 cols, 30.9MB` + only columns
  with nulls (big gaps flagged `!`) + dtype counts. Just paste through `tt trim`.
- **`sklearn` `classification_report`** → one summary line + the worst-recall
  class. Paste through `tt trim`.

These auto-detect inside `tt trim`/`tt clip`, so pasting a big log/JSON/report
into the chat becomes a one-step compress.

**Config for multi-tenant work** (`~/.tokentrim/config.json`):

```json
{
  "resource_include": ["apim_bu_corporate", "rg-prod-corporate"],
  "session_cache": true
}
```

- `resource_include` / `resource_exclude` , regexes that keep/drop resources in
  the Terraform and Cost presets, so you see only your Business Unit.
- `session_cache` , for read-only commands agents love to re-run (`git status`,
  `kubectl get pods`): byte-identical output becomes one line
  (`unchanged since 2m ago`); *slightly* changed output becomes only the changed
  lines (`+`/`-`). Safe: the command still runs; only the display is shortened.

---

## Repo map , fewer exploratory turns

`tt map` prints a one-shot compact overview of the repository: directories,
file counts, and per code file its `def`/`class` signatures. An agent that
reads this once skips the usual burst of `ls`/`cat`/`grep` calls at the start
of a task , and each avoided turn is a full conversation replay you don't pay
for. The generated instruction files tell the agent to do exactly that.

---

## Configuration (optional)

Copy `config.example.json` to `~/.tokentrim/config.json`
(Windows: `%USERPROFILE%\.tokentrim\config.json`). A `.tokentrim.json` in a
repo root overrides it per project:

```json
{
  "exclude": [],           // commands to always leave untouched
  "tee_mode": "failures",  // "failures" | "always" | "never"
  "ultra": false,          // extra-compact output everywhere
  "budget": 0,             // hard cap (~tokens) per command; 0 = off
  "price_per_1k": 0,       // $ per 1K tokens -> `tt gain` shows $ saved
  "session_cache": false   // "unchanged"/diff notes for repeated commands
}
```

---

## Uninstall

```bash
python install.py --uninstall
```

Removes `~/.tokentrim/` and the `tt` launcher. Any
`.github/copilot-instructions.md` files stay in your repos , delete the
TokenTrim section by hand if you want it gone.

---

*Single file · standard library only · Python 3.8+ · Windows / macOS / Linux.*
