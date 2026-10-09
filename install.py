#!/usr/bin/env python3
# Copyright © 2026 Sonomos, Inc.
# SPDX-License-Identifier: Apache-2.0

"""Crux one-shot setup.

Checks prerequisites (and offers to install missing ones), installs the `crux`
CLI with pipx, hooks up git (global or per-repo), scaffolds config (global or
per-repo), and optionally configures Slack. Stdlib only — runnable before crux
itself exists. Safe to re-run: every step is idempotent.

    python3 install.py

Referenced from README.md ("Quick setup").
"""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent
XDG_CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
GLOBAL_CONFIG = XDG_CONFIG_HOME / "crux" / "crux.toml"
# Slack bot token store, read directly by crux.slack (shell-independent).
CREDENTIALS_FILENAME = "credentials.json"

# Human-readable reasons the current shell is stale (PATH/env changes only take
# effect in new shells). Collected as we go; offered as a reload at the end.
RELOAD: list[str] = []

# --- pretty output --------------------------------------------------------
_TTY = sys.stdout.isatty()
def _c(code: str, s: str) -> str:
    return f"\033[{code}m{s}\033[0m" if _TTY else s
BOLD = lambda s: _c("1", s)
DIM = lambda s: _c("2", s)

def say(msg: str = "") -> None:
    print(msg)
def ok(msg: str) -> None:
    print(f"{_c('32', '✓')} {msg}")
def warn(msg: str) -> None:
    print(f"{_c('33', '!')} {msg}")
def err(msg: str) -> None:
    print(f"{_c('31', '✗')} {msg}", file=sys.stderr)
def step(msg: str) -> None:
    print(f"\n{BOLD('== ' + msg + ' ==')}")

def ask(prompt: str, default: bool = False) -> bool:
    hint = "[Y/n]" if default else "[y/N]"
    try:
        reply = input(f"{prompt} {hint} ").strip().lower()
    except EOFError:
        return default
    if not reply:
        return default
    return reply.startswith("y")

def choose(prompt: str, options: dict[str, str], default: str) -> str:
    """Single-key choice. options maps key -> label; returns a key."""
    labels = " / ".join(f"[{k}]{lbl}" for k, lbl in options.items())
    while True:
        try:
            reply = input(f"{prompt} {labels} ({DIM('default ' + default)}): ").strip().lower()
        except EOFError:
            return default
        reply = reply or default
        for k in options:
            if reply == k or reply == options[k].lower():
                return k
        warn(f"Pick one of: {', '.join(options)}")

def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, **kw)

# --- OS-aware package hints ----------------------------------------------
IS_WINDOWS = os.name == "nt"

def detect_pkg_mgr() -> str | None:
    # Windows managers on Windows; the Unix set everywhere else. Crux is fully
    # OS-agnostic now (native Windows/PowerShell included), so setup must be too.
    managers = (("winget", "scoop", "choco") if IS_WINDOWS
                else ("pacman", "dnf", "apt-get", "brew", "zypper"))
    for m in managers:
        if shutil.which(m):
            return m
    return None

PKG_MGR = detect_pkg_mgr()

# tool -> package name/id per manager. winget uses --id (empty = no winget
# package; handled specially, e.g. pipx installs via pip on Windows).
_PKG_NAMES = {
    "rg":      {"pacman": "ripgrep",     "dnf": "ripgrep", "apt-get": "ripgrep", "brew": "ripgrep", "zypper": "ripgrep",     "winget": "BurntSushi.ripgrep.MSVC", "scoop": "ripgrep", "choco": "ripgrep"},
    "gh":      {"pacman": "github-cli",  "dnf": "gh",      "apt-get": "gh",      "brew": "gh",      "zypper": "gh",          "winget": "GitHub.cli",              "scoop": "gh",      "choco": "gh"},
    "pipx":    {"pacman": "python-pipx", "dnf": "pipx",    "apt-get": "pipx",    "brew": "pipx",    "zypper": "python3-pipx", "winget": "",                        "scoop": "pipx",    "choco": "pipx"},
    "git":     {"pacman": "git",         "dnf": "git",     "apt-get": "git",     "brew": "git",     "zypper": "git",         "winget": "Git.Git",                 "scoop": "git",     "choco": "git"},
}

def install_cmd(tool: str) -> list[str] | None:
    """The argv that installs *tool* on this machine, or None if unknown."""
    # pipx has no winget package; pip is the reliable route on Windows.
    if tool == "pipx" and IS_WINDOWS and PKG_MGR not in ("scoop", "choco"):
        return [sys.executable, "-m", "pip", "install", "--user", "pipx"]
    if PKG_MGR is None or tool not in _PKG_NAMES:
        return None
    pkg = _PKG_NAMES[tool].get(PKG_MGR)
    if not pkg:
        return None
    if PKG_MGR == "pacman":
        return ["sudo", "pacman", "-S", "--needed", pkg]
    if PKG_MGR == "dnf":
        return ["sudo", "dnf", "install", "-y", pkg]
    if PKG_MGR == "apt-get":
        return ["sudo", "apt-get", "install", "-y", pkg]
    if PKG_MGR == "brew":
        return ["brew", "install", pkg]
    if PKG_MGR == "zypper":
        return ["sudo", "zypper", "install", "-y", pkg]
    if PKG_MGR == "winget":
        return ["winget", "install", "-e", "--id", pkg]
    if PKG_MGR == "scoop":
        return ["scoop", "install", pkg]
    if PKG_MGR == "choco":
        return ["choco", "install", "-y", pkg]
    return None

def offer_install(tool: str) -> bool:
    """Print the install command for *tool* and offer to run it. Returns True
    if the tool is present afterwards."""
    cmd = install_cmd(tool)
    if cmd is None:
        say(f"  install '{tool}' with your package manager, then re-run this script")
        return False
    say(f"  suggested: {DIM(' '.join(cmd))}")
    if ask(f"  Run it now?", default=False):
        try:
            run(cmd, check=True)
        except (subprocess.CalledProcessError, OSError) as exc:
            err(f"  install failed: {exc}")
            return bool(shutil.which(tool))
        return bool(shutil.which(tool))
    return False

# --- 1. prerequisites -----------------------------------------------------
# `crux super` merges PR heads in memory with `git merge-tree --write-tree`,
# which git gained in 2.38. The rest of Crux runs on older gits, so this is a
# warning with the fix, not a stop. Same floor as crux/superdiff.py MIN_GIT —
# repeated rather than imported because this script runs before crux exists.
SUPER_MIN_GIT = (2, 38)

def git_version() -> tuple[int, int] | None:
    """(major, minor) of the git on PATH, or None when it cannot be read."""
    try:
        res = run(["git", "--version"], capture_output=True, text=True)
    except OSError:
        return None
    match = re.search(r"(\d+)\.(\d+)", res.stdout or "")
    return (int(match.group(1)), int(match.group(2))) if match else None

def check_git_version() -> None:
    have = git_version()
    if have is None or have >= SUPER_MIN_GIT:
        return
    need = ".".join(map(str, SUPER_MIN_GIT))
    warn(f"git {have[0]}.{have[1]} is older than {need} — reviews work, but "
         f"`crux super` needs {need}+ (`git merge-tree --write-tree`)")
    if PKG_MGR == "apt-get":
        # Ubuntu / Pop!_OS 22.04 ship 2.34, and apt will not go further; the
        # git-core PPA (the Ubuntu Git Maintainers team) carries current git.
        ppa = ("sudo add-apt-repository ppa:git-core/ppa && sudo apt update "
               "&& sudo apt install git")
        say(f"  upgrade: {DIM(ppa)}")
    elif (cmd := install_cmd("git")) is not None:
        say(f"  upgrade: {DIM(' '.join(cmd))}")

def check_prereqs() -> None:
    step("Checking prerequisites")
    fatal = False

    # Python: install.py already runs on some python3; crux needs >= 3.11
    # (tomllib), the same floor pyproject.toml declares.
    ver = platform.python_version()
    if sys.version_info >= (3, 11):
        ok(f"python {ver}")
    else:
        warn(f"this interpreter is python {ver}; crux needs >= 3.11 — pipx must "
             "use a 3.11+ python (e.g. pipx install --python python3.11 .)")

    for tool, label in (("git", "git"), ("pipx", "pipx")):
        if shutil.which(tool):
            ok(label)
            if tool == "git":
                check_git_version()
        else:
            err(f"{label} is required.")
            if not offer_install(tool):
                fatal = True
                if tool == "pipx":
                    say(f"  then: {DIM('pipx ensurepath')}  (adds ~/.local/bin to PATH; restart your shell)")

    # Runtime tools: crux installs without them, but reviews need them.
    for tool in ("rg", "gh", "claude"):
        if shutil.which(tool):
            ok(tool)
        elif tool == "claude":
            warn("claude (Claude Code) not found — install it separately; reviews "
                 "and commit enrichment need it, logged in")
        else:
            warn(f"{tool} not found (needed at review time).")
            offer_install(tool)

    if fatal:
        err("Install the required tools above, then re-run: python3 install.py")
        sys.exit(1)

    if shutil.which("gh"):
        res = run(["gh", "auth", "status"], capture_output=True)
        if res.returncode == 0:
            ok("gh authenticated")
        else:
            warn(f"gh is not logged in — run: {DIM('gh auth login')}  (Crux posts PR cards via gh)")

# --- 2. install the crux CLI ---------------------------------------------
def install_cli() -> str | None:
    step("Installing the crux CLI (pipx)")
    existing = shutil.which("crux")
    if existing:
        res = run([existing, "--version"], capture_output=True, text=True)
        ver = res.stdout.strip() or "crux (unknown version)"
        if not ask(f"{ver} is already installed ({existing}). Reinstall / upgrade it?"):
            ok(f"kept {ver}: {existing}")
            return existing
    editable = ["--editable"] if ask("Install in editable/dev mode (tracks this checkout)?") else []
    try:
        run(["pipx", "install", "--force", *editable, str(REPO_DIR)], check=True)
    except (subprocess.CalledProcessError, OSError) as exc:
        err(f"pipx install failed: {exc}")
        sys.exit(1)
    crux = shutil.which("crux")
    if crux is None:
        cand = Path.home() / ".local" / "bin" / "crux"
        crux = str(cand) if cand.exists() else None
    if crux:
        ok(f"crux installed: {crux}")
        return crux
    warn("crux installed but not on PATH.")
    if ask("Run `pipx ensurepath` to add pipx's bin dir to your PATH?", default=True):
        try:
            run(["pipx", "ensurepath"], check=True)
            RELOAD.append("pipx added its bin dir to your PATH")
        except (subprocess.CalledProcessError, OSError) as exc:
            err(f"pipx ensurepath failed: {exc}")
    cand = Path.home() / ".local" / "bin" / "crux"
    return str(cand) if cand.exists() else None

# --- 3. hooks -------------------------------------------------------------
def _crux_prepush(hooks_dir: Path) -> bool:
    """True when hooks_dir already holds a crux pre-push hook."""
    p = hooks_dir / "pre-push"
    try:
        return p.is_file() and "crux" in p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False

def setup_hooks(crux: str | None) -> None:
    step("Hooking up git")
    if crux is None:
        warn("crux not on PATH yet — skipping. After fixing PATH, run: crux install-hooks")
        return
    # If crux hooks are already installed globally, default to skipping so a
    # re-run of install.py leaves them alone unless you say otherwise.
    res = run(["git", "config", "--global", "--get", "core.hooksPath"],
              capture_output=True, text=True)
    gpath = res.stdout.strip() if res.returncode == 0 else ""
    already_global = bool(gpath and _crux_prepush(Path(gpath).expanduser()))
    if already_global:
        ok(f"crux hooks already installed globally: {gpath}")
    where = choose("Install hooks where?",
                   {"g": "global (every repo)", "l": "local (this repo)", "s": "skip"},
                   "s" if already_global else "g")
    if where == "s":
        say(f"Skipped. Later: {DIM('crux install-hooks')} (global) or {DIM('crux install-hooks --local')} (a repo).")
        return
    cmd = [crux, "install-hooks"] + (["--local"] if where == "l" else [])
    try:
        run(cmd, check=True)
    except (subprocess.CalledProcessError, OSError) as exc:
        err(f"install-hooks failed: {exc}")

# --- 4. config ------------------------------------------------------------
def _scaffold_config(dest: Path, note: str) -> None:
    if dest.exists():
        ok(f"{note} config already exists: {dest} (left untouched)")
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO_DIR / "crux.toml.example", dest)
        ok(f"wrote {dest} (from crux.toml.example)")
        _ask_scope_owners(dest)

def _ask_scope_owners(dest: Path) -> None:
    """Fill [scope] owners. Crux has no built-in owner, so until this is set
    it does nothing on any repo."""
    reply = input("  Which GitHub orgs/users should Crux run on? "
                  f"{DIM('(comma-separated, e.g. my-org, my-username)')}: ").strip()
    owners = [o.strip() for o in reply.split(",") if o.strip()]
    if not owners:
        warn(f"No owners set — Crux will do nothing until you edit [scope] owners in {dest}")
        return
    text = dest.read_text(encoding="utf-8")
    listed = ", ".join(f'"{o}"' for o in owners)
    dest.write_text(text.replace("owners = []", f"owners = [{listed}]", 1), encoding="utf-8")
    ok(f"Crux will run on repos owned by: {', '.join(owners)}")

def setup_config() -> None:
    step("Scaffolding config")
    local_path = REPO_DIR / "crux.toml"
    have_global, have_local = GLOBAL_CONFIG.exists(), local_path.exists()
    if have_global:
        ok(f"global config already exists: {GLOBAL_CONFIG}")
    if have_local:
        ok(f"local config already exists: {local_path}")
    # If a global config is already in place (the common case on a re-run),
    # default to skipping so nothing is redone unless you ask.
    where = choose("Config where?",
                   {"g": "global (every repo)", "l": "local (this repo)", "b": "both", "s": "skip"},
                   "s" if have_global else "g")
    if where in ("g", "b"):
        _scaffold_config(GLOBAL_CONFIG, "global")
    if where in ("l", "b"):
        _scaffold_config(local_path, "local")
    if where == "s":
        say("Skipped. Copy crux.toml.example to ~/.config/crux/crux.toml or a repo's crux.toml later.")

# --- 5. Super PRs (optional) ----------------------------------------------
# The example file is the single source of this block's text: install.py grafts
# that exact section onto configs written before D37, so an upgrading user gets
# the same commented documentation a fresh install does.
_SUPER_MARKER = "# --- Super PRs (D37)"

def _super_block() -> str:
    """The [super] section of crux.toml.example, verbatim."""
    text = (REPO_DIR / "crux.toml.example").read_text(encoding="utf-8")
    index = text.find(_SUPER_MARKER)
    return text[index:] if index >= 0 else ""

def _existing_configs() -> list[Path]:
    return [p for p in (GLOBAL_CONFIG, REPO_DIR / "crux.toml") if p.exists()]

def _scope_owner(path: Path) -> str:
    """First [scope] owners entry, for defaulting the home repo's owner."""
    try:
        import tomllib
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return ""
    owners = (data.get("scope") or {}).get("owners") or []
    return owners[0] if owners and isinstance(owners[0], str) else ""

def _write_super_home(path: Path, home: str) -> None:
    """Set [super] home in a config, adding the whole block if it has none.

    A config written before D37 has no [super] table at all, so appending the
    documented block is the upgrade path — the alternative is a user who
    updates Crux and never discovers the feature exists.
    """
    text = path.read_text(encoding="utf-8")
    if _SUPER_MARKER not in text and "[super]" not in text:
        block = _super_block()
        if block:
            lead = "" if text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
            with path.open("a", encoding="utf-8") as fh:
                fh.write(lead + block)
            text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    in_super = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("["):
            in_super = stripped == "[super]"
            continue
        if in_super and stripped.startswith("home") and "=" in stripped.split("#", 1)[0]:
            lines[i] = f'home  = "{home}"   # set by install.py'
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return
    with path.open("a", encoding="utf-8") as fh:
        fh.write(f'\n[super]\nhome = "{home}"\n')

def setup_super() -> None:
    step("Super PRs (optional)")
    say("A super PR bundles one feature's PRs across several repos into a")
    say("single brief, and merges them all with one command.")
    configs = _existing_configs()
    if not configs:
        say(DIM("No config to update — re-run after scaffolding one."))
        return
    if not ask("Set up super PRs?", default=False):
        say(DIM("Skipped. Set [super] home in crux.toml whenever you want it."))
        return

    owner = next((o for o in map(_scope_owner, configs) if o), "")
    suggested = f"{owner}/pr-bundles" if owner else ""
    prompt = f"Repo to hold the brief issues [{suggested}]: " if suggested \
        else "Repo to hold the brief issues (owner/name): "
    home = input(prompt).strip() or suggested
    if "/" not in home:
        warn("Need an owner/name — skipping. Set [super] home by hand later.")
        return

    # The brief is a GitHub issue, so the repo has to exist. Checking here beats
    # a confusing failure on the first `crux super new`.
    #
    # `gh` may not exist at all: cloud containers ship without it, and the
    # README now prescribes the setup script as the reliable install route
    # there, so this prompt IS on the recommended path. run() is a bare
    # subprocess.run and __main__ catches only KeyboardInterrupt, so an
    # unguarded call here ends the installer in a FileNotFoundError traceback.
    # Record the choice and move on instead — `crux super` needs gh anyway.
    if not shutil.which("gh"):
        warn(f"gh is not installed, so {home} cannot be verified or created "
             f"here — recording it anyway. `crux super` needs gh; install it "
             f"before using super PRs.")
        for path in configs:
            _write_super_home(path, home)
            ok(f"set [super] home in {path}")
        return
    exists = run(["gh", "repo", "view", home], capture_output=True).returncode == 0
    if not exists:
        warn(f"{home} does not exist or is not visible to you.")
        if ask(f"Create {home} as a private repo now?", default=False):
            made = run(["gh", "repo", "create", home, "--private"],
                       capture_output=True)
            if made.returncode == 0:
                ok(f"created {home}")
            else:
                err(f"could not create it: {made.stderr.decode(errors='replace').strip()}")
                return
        else:
            say(DIM("Create it later; super PRs will not work until it exists."))

    for path in configs:
        _write_super_home(path, home)
        ok(f"set [super] home in {path}")
    say(f"  {DIM('Super PRs draw from the repos in [scope] — nothing else to configure.')}")
    say(f"  {DIM('Run `crux super new` to pick the PRs that make up one.')}")

# --- 6. Slack (optional) --------------------------------------------------
def _slack_token_valid(token: str) -> tuple[bool, str]:
    """Call auth.test (needs no scopes). Returns (ok, detail)."""
    req = urllib.request.Request(
        "https://slack.com/api/auth.test",
        headers={"Authorization": f"Bearer {token}"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # network/parse — don't block setup
        return False, f"could not reach Slack ({exc})"
    if body.get("ok"):
        return True, body.get("team", "")
    return False, body.get("error", "unknown error")

def _write_slack_channel(config_path: Path, channel: str) -> None:
    """Set [slack] channel in an existing config (the template ships
    `channel = ""`), or append a [slack] table if it has none."""
    text = config_path.read_text(encoding="utf-8")
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.lstrip().startswith("channel") and "=" in line.split("#", 1)[0]:
            lines[i] = f'channel = "{channel}"    # set by install.py'
            config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return
    with config_path.open("a", encoding="utf-8") as fh:
        fh.write(f'\n[slack]\nchannel = "{channel}"\ntoken_env = "SLACK_BOT_TOKEN"\n')

def _persist_token(token: str) -> None:
    """Store the Slack bot token in the crux credentials file
    (~/.config/crux/credentials.json, mode 0600), which Crux reads directly.

    This is shell-independent by design: unlike the old ~/.bashrc export, it
    works under any shell (zsh/fish/PowerShell) and when a push comes from an
    IDE/GUI/agent with no shell env loaded — and needs no shell reload to take
    effect. Written here directly (not via `import crux`) so install.py stays
    stdlib-only. Unknown keys already in the file are preserved."""
    path = XDG_CONFIG_HOME / "crux" / CREDENTIALS_FILENAME
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    data["slack_bot_token"] = token
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)  # no-op on Windows
        except OSError:
            pass
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except OSError as exc:
        err(f"  could not write {path}: {exc}")
        return
    ok(f"saved the Slack bot token to {path}")
    say(DIM("  Crux reads it directly — no shell reload, works in any shell."))

def setup_slack() -> None:
    step("Slack notifications (optional)")
    say("Crux can announce each PR to a Slack channel and thread later updates.")
    if not ask("Set up Slack now?"):
        say("Skipped. Configure it later via the README 'Slack notifications' section.")
        return

    if ask("Do you already have a Slack app with a bot token?"):
        say(f"Grab its token: your Slack app → {BOLD('OAuth & Permissions')} →")
        say(f"  copy the {BOLD('Bot User OAuth Token')} (starts with {DIM('xoxb-')}).")
    else:
        say("Create one — it takes about two minutes:")
        say(f"  1. Open {BOLD('https://api.slack.com/apps')} → {BOLD('Create New App')} → "
            f"{BOLD('From scratch')}; pick your workspace.")
        say(f"  2. {BOLD('OAuth & Permissions')} → {BOLD('Bot Token Scopes')}, add:")
        say(f"       {DIM('chat:write')}        post messages")
        say(f"       {DIM('channels:history')}  detect an existing PR link ({DIM('groups:history')} for private)")
        say(f"       {DIM('channels:read')}     resolve a channel NAME (skip if you use a channel ID)")
        say(f"  3. {BOLD('Install to Workspace')}, then copy the {BOLD('Bot User OAuth Token')} ({DIM('xoxb-')}…).")
        say(f"  4. In Slack, invite the bot to the channel:  {DIM('/invite @YourApp')}")
        try:
            input("Press enter once you have the xoxb-… token… ")
        except EOFError:
            pass

    try:
        channel = input("  Channel name or ID (e.g. pull-requests or C0123456): ").strip()
    except EOFError:
        channel = ""
    try:
        import getpass
        token = getpass.getpass("  Bot token (xoxb-…, hidden): ").strip()
    except (EOFError, Exception):
        token = ""

    if not channel or not token:
        warn("Channel or token empty — skipping Slack setup.")
        return

    valid, detail = _slack_token_valid(token)
    if valid:
        ok(f"token valid{f' (workspace: {detail})' if detail else ''}")
    else:
        warn(f"Slack rejected the token ({detail}) — saving config anyway; fix the token before pushing.")

    # Channel lives in a config file; the token is read from $SLACK_BOT_TOKEN
    # at push time and is never stored in config.
    target = GLOBAL_CONFIG
    if not target.exists():
        # No global config yet — make one so there is somewhere to put channel.
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO_DIR / "crux.toml.example", target)
    _write_slack_channel(target, channel)
    ok(f'set [slack] channel = "{channel}" in {target}')
    # The token goes in the credentials file, which Crux reads directly — so
    # there is nothing to reload and no shell dependency to warn about.
    _persist_token(token)

def _zenhub_key_valid(key: str) -> tuple[bool, str]:
    """Ask Zenhub who the key belongs to. Returns (ok, detail)."""
    body = json.dumps({"query": "query { viewer { id } }"}).encode("utf-8")
    req = urllib.request.Request(
        "https://api.zenhub.com/public/graphql", data=body,
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            answer = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # network/parse — don't block setup
        return False, f"could not reach Zenhub ({exc})"
    if answer.get("errors"):
        first = (answer["errors"] or [{}])[0]
        return False, str(first.get("message", "rejected"))
    return True, ""


def _write_zenhub_workspace(config_path: Path, workspace: str, pipeline: str) -> None:
    """Set [zenhub] workspace/done_pipeline in an existing config (the template
    ships them empty), or append a [zenhub] table if it has none."""
    text = config_path.read_text(encoding="utf-8")
    lines = text.splitlines()
    in_zenhub = False
    wrote = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("["):
            in_zenhub = stripped == "[zenhub]"
            continue
        if not in_zenhub or "=" not in stripped.split("#", 1)[0]:
            continue
        key = stripped.split("=", 1)[0].strip()
        if key == "workspace":
            lines[i] = f'workspace = "{workspace}"    # set by install.py'
            wrote = True
        elif key == "done_pipeline":
            lines[i] = f'done_pipeline = "{pipeline}"'
    if wrote:
        config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return
    with config_path.open("a", encoding="utf-8") as fh:
        fh.write(f'\n[zenhub]\nworkspace = "{workspace}"\n'
                 f'done_pipeline = "{pipeline}"\n')


def _persist_zenhub_key(key: str) -> None:
    """Store the Zenhub API key in the crux credentials file (0600), the same
    shell-independent store the Slack token uses. Written here directly (not
    via `import crux`) so install.py stays stdlib-only; unknown keys already in
    the file are preserved."""
    path = XDG_CONFIG_HOME / "crux" / CREDENTIALS_FILENAME
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    data["zenhub_api_key"] = key
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)  # no-op on Windows
        except OSError:
            pass
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except OSError as exc:
        err(f"  could not write {path}: {exc}")
        return
    ok(f"saved the Zenhub API key to {path}")


def setup_zenhub() -> None:
    step("Zenhub (optional)")
    say("Crux can link the Zenhub tickets a PR closes and close them when it "
        "lands —")
    say("including across repos, where GitHub's own `Closes #N` does not reach.")
    if not ask("Set up Zenhub now?"):
        say("Skipped. Configure it later with `crux zenhub setup`, or the "
            "README 'Zenhub' section.")
        return

    say(f"  1. Open {BOLD('https://app.zenhub.com/settings/tokens')} → "
        f"{BOLD('Create a new API key')}.")
    say(f"  2. Copy it now — Zenhub shows it {BOLD('once')}.")
    try:
        import getpass
        key = getpass.getpass("  Zenhub API key (hidden): ").strip()
    except (EOFError, Exception):
        key = ""
    if not key:
        warn("No key given — skipping Zenhub setup.")
        return

    valid, detail = _zenhub_key_valid(key)
    if valid:
        ok("key valid")
    else:
        warn(f"Zenhub rejected the key ({detail}) — saving anyway; fix it "
             f"before linking.")

    try:
        workspace = input("  Workspace name (as it appears in Zenhub): ").strip()
    except EOFError:
        workspace = ""
    if not workspace:
        warn("No workspace given — the key is saved, but Zenhub stays OFF "
             "until [zenhub] workspace is set.")
        _persist_zenhub_key(key)
        return
    try:
        pipeline = input('  Pipeline for closed cards [Closed]: ').strip() or "Closed"
    except EOFError:
        pipeline = "Closed"

    target = GLOBAL_CONFIG
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO_DIR / "crux.toml.example", target)
    _write_zenhub_workspace(target, workspace, pipeline)
    ok(f'set [zenhub] workspace = "{workspace}" in {target}')
    _persist_zenhub_key(key)
    say(DIM("  Check it anytime: `crux zenhub status` / `crux zenhub doctor`."))


# --- shell reload ---------------------------------------------------------
def finish_reload() -> None:
    """Env changes (PATH, SLACK_BOT_TOKEN) only reach NEW shells — a subprocess
    can't mutate its parent. Tell the user exactly what's stale and how to load
    it, so e.g. Slack (which reads $SLACK_BOT_TOKEN at push time) actually works."""
    if not RELOAD:
        return
    step("One more step: reload your shell")
    warn("These changes only take effect in a new shell:")
    for reason in RELOAD:
        say(f"  - {reason}")
    say("")
    if IS_WINDOWS:
        say("Load them by opening a new terminal (PowerShell or Windows "
            "Terminal).")
    else:
        say(f"Load them now by either:")
        say(f"  • running  {DIM('source ~/.bashrc')}  (or your shell's rc)  in this shell, or")
        say(f"  • opening a new terminal.")

# --- main -----------------------------------------------------------------
def main() -> None:
    say(BOLD("Crux setup"))
    say(DIM(f"repo: {REPO_DIR}"))
    check_prereqs()
    crux = install_cli()
    setup_hooks(crux)
    setup_config()
    setup_super()
    setup_slack()
    setup_zenhub()
    step("Done")
    ok("Crux is set up.")
    say(f"Next: from a repo whose owner is in [scope] owners, {DIM('git push')} — "
        "Crux reviews the PR in the background.")
    say(f"Verify anytime: {DIM('crux --help')} and {DIM('crux preview')} (prints a card without posting).")
    finish_reload()  # last: may replace this process with a fresh shell

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
        err("Cancelled.")
        sys.exit(130)
