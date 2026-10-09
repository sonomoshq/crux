# Copyright © 2026 Sonomos, Inc.
# SPDX-License-Identifier: Apache-2.0

"""claude_autorun (plugin PostToolUse hook): trigger detection, D13 scope
gating (including the new [scope] repos allowlist), git-hook dedupe, and the
never-crash contract."""
from __future__ import annotations

import io
import json
import logging
from types import SimpleNamespace

import crux.claude_autorun as autorun
import crux.cli as cli
import crux.config as config
from crux.models import Config


def _payload(command: str | None = "git push", tool: str = "Bash",
             cwd: str = ".") -> dict:
    data: dict = {"hook_event_name": "PostToolUse", "tool_name": tool,
                  "cwd": cwd}
    if tool == "Bash":
        data["tool_input"] = {"command": command}
    return data


# -- _trigger ----------------------------------------------------------------

def test_trigger_git_push():
    assert autorun._trigger(_payload("git push origin HEAD")) == "push"


def test_trigger_compound_command():
    assert autorun._trigger(_payload("git add -A && git commit -m x && "
                                     "git push -u origin main")) == "push"


def test_trigger_ignores_dry_run_and_unrelated():
    assert autorun._trigger(_payload("git push --dry-run")) is None
    assert autorun._trigger(_payload("git status")) is None
    # "push" in a different pipeline segment is not a git push
    assert autorun._trigger(_payload("git log | grep push")) is None


def test_trigger_gh_pr_create():
    assert autorun._trigger(_payload("gh pr create --fill")) == "pr-create"
    # quoted arguments around the real invocation must not hide it
    assert autorun._trigger(_payload(
        'gh pr create --title "add push support" --body "see gh pr create"'
    )) == "pr-create"


def test_trigger_ignores_commands_named_only_inside_quotes():
    """A commit message that mentions a trigger is not that trigger.

    Observed in the field: committing with -m "...detaches a crux run on
    `gh pr create`..." detached a review run off a plain `git commit`.
    """
    assert autorun._trigger(_payload(
        'git commit -m "verify the hook detaches a run on `gh pr create`"'
    )) is None
    assert autorun._trigger(_payload(
        "git commit -m 'fix the git push detection path'"
    )) is None
    assert autorun._trigger(_payload(
        'git commit -m "multi\nline mentioning gh pr create"'
    )) is None
    # a real push carrying such a message still counts
    assert autorun._trigger(_payload(
        'git commit -m "about gh pr create" && git push origin HEAD'
    )) == "push"


def test_trigger_ignores_mentions_quote_stripping_cannot_catch():
    """Prose mentions survive _unquoted() and must still be rejected.

    An apostrophe unbalances quote pairing and heredoc bodies are unquoted,
    so the command-position anchor is what rejects these. Both cases are
    real: the second is the commit that landed this fix.
    """
    assert autorun._trigger(_payload(
        "python - <<'EOF'\nreal = 'gh pr create --fill'  # the plugin's case\nEOF"
    )) is None
    assert autorun._trigger(_payload(
        'git commit -F - <<MSG\nThe same bug sat in _PUSH_RE, which matches\n'
        '`git ... push` within one pipeline segment.\nMSG'
    )) is None


def test_trigger_allows_command_prefixes_and_shell_keywords():
    """Anchoring must not reject the ways a real invocation gets prefixed."""
    assert autorun._trigger(_payload("cd /repo && gh pr create --fill")) == "pr-create"
    assert autorun._trigger(_payload("GH_TOKEN=x gh pr create --fill")) == "pr-create"
    assert autorun._trigger(_payload(
        "for r in a b; do git push $r; done")) == "push"
    assert autorun._trigger(_payload("(cd /repo; git push origin HEAD)")) == "push"


def test_trigger_mcp_tools():
    assert autorun._trigger(_payload(
        tool="mcp__github__create_pull_request")) == "pr-create"
    assert autorun._trigger(_payload(
        tool="mcp__github__push_files")) == "pr-create"
    assert autorun._trigger(_payload(tool="mcp__github__get_me")) is None


# -- run() end to end (spawn injected) ---------------------------------------

def _scoped(monkeypatch, tmp_path, covered: bool):
    """Point _hook_scope at a dummy in-scope repo rooted at tmp_path."""
    info = SimpleNamespace(root=str(tmp_path), owner="example-org", repo="crux")
    monkeypatch.setattr(cli, "_hook_scope", lambda log: (info, Config()))
    monkeypatch.setattr(autorun, "_git_hook_covers_push",
                        lambda root: covered)


def _run(payload: dict) -> list[list[str]]:
    calls: list[list[str]] = []
    rc = autorun.run(io.StringIO(json.dumps(payload)),
                     spawn=lambda args, log: calls.append(args))
    assert rc == 0
    return calls


def test_run_spawns_review_for_push(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    _scoped(monkeypatch, tmp_path, covered=False)
    calls = _run(_payload("git push", cwd=str(tmp_path)))
    assert calls == [["run", "--delay", "15", "--yes"]]


def test_run_skips_push_when_git_hook_covers(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    _scoped(monkeypatch, tmp_path, covered=True)
    assert _run(_payload("git push", cwd=str(tmp_path))) == []
    # PR creation never goes through the git pre-push hook: still spawns.
    assert _run(_payload("gh pr create", cwd=str(tmp_path))) == [
        ["run", "--delay", "15", "--yes"]]


def test_run_out_of_scope_does_nothing(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_hook_scope", lambda log: None)
    assert _run(_payload("git push", cwd=str(tmp_path))) == []


def test_run_swallows_garbage_and_missing_cwd(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert autorun.run(io.StringIO("not json"),
                       spawn=lambda a, l: None) == 0
    assert autorun.run(io.StringIO(json.dumps(
        _payload("git push", cwd=str(tmp_path / "gone")))),
        spawn=lambda a, l: None) == 0


# -- [scope] repos (D13 allowlist) -------------------------------------------

def test_config_maps_scope_repos(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "crux.toml").write_text(
        '[scope]\nrepos = ["example-org/crux"]\n', encoding="utf-8")
    assert config.load(repo).scope_repos == ["example-org/crux"]


def test_hook_scope_honors_repo_allowlist(monkeypatch, tmp_path):
    log = logging.getLogger("test-crux-scope")
    info = SimpleNamespace(root=str(tmp_path), owner="example-org",
                           repo="Other")
    cfg = Config(scope_owners=["example-org"])
    cfg.scope_repos = ["example-org/crux"]
    monkeypatch.setattr("crux.gitio.repo_info", lambda: info)
    monkeypatch.setattr("crux.config.load", lambda root: cfg)
    assert cli._hook_scope(log) is None

    # case-insensitive match lets the repo through
    cfg.scope_repos = ["Example-Org/OTHER"]
    assert cli._hook_scope(log) is not None

    # empty allowlist = every repo of an allowed owner (the old behavior)
    cfg.scope_repos = []
    assert cli._hook_scope(log) is not None


def test_no_owners_configured_means_nothing_runs_and_says_how(monkeypatch, tmp_path, caplog):
    # There is no built-in owner: an empty [scope] owners does nothing, and
    # says how to turn Crux on instead of staying silent.
    log = logging.getLogger("test-crux-scope")
    info = SimpleNamespace(root=str(tmp_path), owner="someone", repo="app")
    monkeypatch.setattr("crux.gitio.repo_info", lambda: info)
    monkeypatch.setattr("crux.config.load", lambda root: Config())
    with caplog.at_level(logging.WARNING):
        assert cli._hook_scope(log) is None
    assert 'owners = ["someone"]' in caplog.text
