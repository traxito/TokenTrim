#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TokenTrim installer -- zero dependencies, works on Windows / macOS / Linux.

What it does:
  1. Copies tt.py to ~/.tokentrim/tt.py
  2. Creates a `tt` launcher so you can type `tt ...` from any terminal
       * Linux/macOS: ~/.local/bin/tt   (a tiny shell wrapper)
       * Windows:     %USERPROFILE%\\.tokentrim\\bin\\tt.cmd  and  tt.ps1
  3. Writes the GitHub Copilot integration file into the current repo:
       .github/copilot-instructions.md   (use --global for user-wide, --no-copilot to skip)

Usage:
    python install.py                 # install + Copilot file in current repo
    python install.py --global        # install + user-wide Copilot instructions
    python install.py --no-copilot    # install the CLI only
    python install.py --uninstall     # remove ~/.tokentrim and the launcher

Nothing is downloaded and no admin rights are needed. On Windows the PATH is not
modified automatically (to avoid corrupting it); the script prints the exact
one-line command to add the launcher folder to your PATH.
"""
from __future__ import annotations

import os
import shutil
import stat
import sys
from pathlib import Path

HOME = Path(os.path.expanduser("~"))
TT_DIR = HOME / ".tokentrim"
IS_WIN = os.name == "nt"
HERE = Path(__file__).resolve().parent


def _say(msg=""):
    sys.stdout.write(msg + "\n")


def _on_path(directory):
    p = os.environ.get("PATH", "")
    parts = p.split(os.pathsep)
    d = str(directory)
    return any(os.path.normcase(os.path.normpath(x)) == os.path.normcase(d)
               for x in parts if x)


def install(copilot="repo"):
    src = HERE / "tt.py"
    if not src.exists():
        _say("ERROR: tt.py not found next to install.py (%s)." % HERE)
        return 1

    TT_DIR.mkdir(parents=True, exist_ok=True)
    dst = TT_DIR / "tt.py"
    shutil.copyfile(str(src), str(dst))
    _say("Installed engine -> %s" % dst)

    launcher_dir, launcher_hint = _create_launcher(dst)

    # Copilot integration (delegates to `tt init`, which owns the text).
    if copilot != "none":
        _say("")
        args = ["init"] + (["--global"] if copilot == "global" else [])
        try:
            sys.dont_write_bytecode = True  # don't litter ~/.tokentrim
            import importlib.util
            spec = importlib.util.spec_from_file_location("tt_installed", str(dst))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            mod.cmd_init(args[1:])
        except Exception as exc:
            _say("Could not write Copilot instructions automatically: %s" % exc)
            _say("Run this later from your repo:  tt init")

    _say("")
    _say("=" * 60)
    _say("TokenTrim installed.")
    _say("=" * 60)
    if launcher_hint:
        _say(launcher_hint)
    _say("")
    _say("Try it:")
    _say("    tt git status")
    _say("    tt gain")
    return 0


def _create_launcher(engine_path):
    """Create a `tt` launcher. Returns (launcher_dir, hint_text)."""
    if IS_WIN:
        bindir = TT_DIR / "bin"
        bindir.mkdir(parents=True, exist_ok=True)
        cmd = bindir / "tt.cmd"
        cmd.write_text(
            "@echo off\r\n"
            'python "%USERPROFILE%\\.tokentrim\\tt.py" %*\r\n',
            encoding="utf-8",
        )
        ps1 = bindir / "tt.ps1"
        ps1.write_text(
            'python "$env:USERPROFILE\\.tokentrim\\tt.py" @args\r\n',
            encoding="utf-8",
        )
        _say("Created launcher -> %s" % cmd)
        if _on_path(bindir):
            return bindir, "The `tt` command is ready (launcher folder already on PATH)."
        # NOTE: deliberately NOT `setx PATH "%PATH%;..."` -- setx truncates at
        # 1024 chars and would bake the machine PATH into the user PATH.
        hint = (
            "Add the launcher to your *user* PATH (run once in PowerShell), "
            "then reopen your terminal:\n"
            "    [Environment]::SetEnvironmentVariable('Path', "
            "[Environment]::GetEnvironmentVariable('Path','User') + ';%s', 'User')"
            % bindir
        )
        return bindir, hint

    # Unix
    bindir = HOME / ".local" / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    launcher = bindir / "tt"
    launcher.write_text(
        "#!/bin/sh\n"
        'exec python3 "%s" "$@"\n' % engine_path,
        encoding="utf-8",
    )
    launcher.chmod(launcher.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP |
                   stat.S_IXOTH)
    _say("Created launcher -> %s" % launcher)
    if _on_path(bindir):
        return bindir, "The `tt` command is ready (%s is on your PATH)." % bindir
    shellrc = "~/.zshrc" if os.environ.get("SHELL", "").endswith("zsh") \
        else "~/.bashrc"
    hint = (
        "Add ~/.local/bin to your PATH (run once), then reopen your terminal:\n"
        "    echo 'export PATH=\"$HOME/.local/bin:$PATH\"' >> %s" % shellrc
    )
    return bindir, hint


def uninstall():
    removed = []
    # launcher(s)
    if IS_WIN:
        for name in ("bin/tt.cmd", "bin/tt.ps1"):
            p = TT_DIR / name
            if p.exists():
                p.unlink()
                removed.append(str(p))
    else:
        p = HOME / ".local" / "bin" / "tt"
        if p.exists():
            p.unlink()
            removed.append(str(p))
    # engine + state
    if TT_DIR.exists():
        shutil.rmtree(str(TT_DIR), ignore_errors=True)
        removed.append(str(TT_DIR))
    if removed:
        _say("Removed:")
        for r in removed:
            _say("    " + r)
    else:
        _say("Nothing to remove.")
    _say("")
    _say("Note: any .github/copilot-instructions.md files were left in place "
         "(they live in your repos). Delete the TokenTrim section manually if "
         "you want it gone.")
    return 0


def main(argv):
    if "--uninstall" in argv:
        return uninstall()
    if "--help" in argv or "-h" in argv:
        _say(__doc__)
        return 0
    copilot = "repo"
    if "--no-copilot" in argv:
        copilot = "none"
    elif "--global" in argv or "-g" in argv:
        copilot = "global"
    return install(copilot)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
