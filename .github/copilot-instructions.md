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
