# Copyright © 2026 Sonomos, Inc.
# SPDX-License-Identifier: Apache-2.0

"""PR discovery/creation and sticky-comment upsert via the `gh` CLI (D2, D8, D11).

Comment bodies are sent to `gh api` as a full JSON request payload on stdin via
`--input -`. Mechanism note (required by spec): `-f body=@-` does NOT work,
because `-f/--raw-field` never expands the `@file` syntax — only `-F/--field`
does. `--input -` is gh's documented way to supply a request body from stdin;
it needs no shell quoting, has no argv length limit, gets proper JSON escaping
from json.dumps, and behaves identically for PATCH and POST. That is the
mechanism used here.

Hosts without `gh`: every call in THIS module degrades to a direct REST
request through crux.ghrest, authenticated by a user-supplied token (GH_TOKEN
/ GITHUB_TOKEN / the crux credentials file). `gh api` invocations translate
mechanically — the argv already names the method, path and body — and the
`gh pr …` conveniences (find_pr, pr_meta, create_pr) carry an explicit REST
equivalent (see _run_gh's *rest* parameter). A host with gh installed never
takes this path, even when gh is unauthenticated: gh's own error is more
actionable there.

The claim is scoped to this module on purpose. Other callers of _run_gh still
issue non-`api` argv with no REST equivalent, and those DO fail on a gh-less
host: `crux prs` (crux.prs — `gh repo list`, `gh pr list --repo`) and
`crux super`'s `gh repo clone` (crux.superact). Whether a token would even
help depends on the host: on a generic gh-less host (CI runner, plain
container) it does; in a claude.ai/code session the egress proxy blocks raw
writes to api.github.com no matter the token, so the review card is posted by
the session itself through its GitHub MCP tools (README, `/crux:run`).
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import urllib.parse
from collections.abc import Iterator
from pathlib import Path

from crux.models import CARD_MARKER, TEST_MARKER, Config, PostError, RepoInfo

log = logging.getLogger("crux.post")

_STDERR_EXCERPT_CHARS = 300
# gh runs on the pre-push FOREGROUND path (find_pr via `crux ensure-pr`), so a
# stalled network must never hang the push: short timeout there, longer for
# background operations (comment upsert, PR create in the detached run).
_GH_FOREGROUND_TIMEOUT = 15
_GH_BACKGROUND_TIMEOUT = 120


def _run_gh(args: list[str], cwd: str | None = None, stdin_text: str | None = None,
            timeout: int = _GH_BACKGROUND_TIMEOUT,
            rest: tuple | None = None) -> str:
    """Run `gh <args>` and return stdout. Any failure raises PostError.

    When `gh` is not installed the call is re-issued as a direct REST request
    (crux.ghrest) and the response text is returned in gh's stdout shape.
    *rest* — ``("METHOD", "path")`` or ``("METHOD", "path", payload_dict)`` —
    names the endpoint for `gh pr …` conveniences, whose argv doesn't spell it
    out; plain `gh api` argv translates without it. A non-api call with no
    *rest* keeps the old "gh not installed" error.
    """
    try:
        proc = subprocess.run(
            ["gh", *args],
            capture_output=True,
            encoding="utf-8", errors="replace",
            input=stdin_text,
            cwd=cwd,
            timeout=timeout,
        )
    except FileNotFoundError:
        return _rest_fallback(args, stdin_text, timeout, rest)
    except subprocess.TimeoutExpired:
        raise PostError(f"gh {' '.join(args[:2])} timed out after {timeout}s") from None
    if proc.returncode != 0:
        excerpt = " ".join((proc.stderr or "").strip().split())[:_STDERR_EXCERPT_CHARS]
        raise PostError(f"gh {' '.join(args[:2])} failed: {excerpt}")
    return proc.stdout


def _rest_fallback(args: list[str], stdin_text: str | None, timeout: int,
                   rest: tuple | None) -> str:
    """The gh-less path of _run_gh: the same operation as a REST request.

    An explicit *rest* spec wins; otherwise a `gh api` argv is decoded in
    place — `-X` is the method (default GET), the first non-flag argument the
    path, `--paginate` carries over, an `--input -` body is *stdin_text*
    exactly as it would have reached gh, and `--jq` is applied to the response
    (see _apply_jq) so a caller asking for one field still gets one field.
    """
    from crux import ghrest
    if rest is not None:
        method, path, *payload = rest
        body = json.dumps(payload[0]) if payload else None
        return ghrest.rest_call(method, path, body, timeout=timeout)
    if args and args[0] == "api":
        method, path, paginate, jq = "GET", None, False, None
        rest_args = iter(args[1:])
        for arg in rest_args:
            if arg == "-X":
                method = next(rest_args, "GET")
            elif arg == "--paginate":
                paginate = True
            elif arg == "--jq":
                jq = next(rest_args, None)
            elif arg == "--input":
                next(rest_args, None)  # always "-"; the body is stdin_text
            elif not arg.startswith("-") and path is None:
                path = arg
        if path:
            out = ghrest.rest_call(method, path, stdin_text,
                                   paginate=paginate, timeout=timeout)
            return _apply_jq(jq, out) if jq else out
    raise PostError("gh not installed")


# `gh api --jq` runs a whole jq program; every use of it in Crux is a plain
# dotted field path (".body", ".base.ref", ".user.login", ".permissions.admin").
# The REST path applies exactly that much and refuses anything richer — a
# caller that asked for one field must never silently receive the entire JSON
# document instead, which is how a merge gate reading `.user.login` would come
# back with a string that matches nobody.
_JQ_FIELD = r"\.[A-Za-z_][A-Za-z0-9_]*"
_JQ_FIELD_PATH_RE = re.compile(rf"{_JQ_FIELD}(?:{_JQ_FIELD})*\Z")


def _apply_jq(program: str | None, out: str) -> str:
    """gh's `--jq <dotted.path>` output, applied to a REST response body.

    Matches gh for the shapes Crux uses: a string prints raw (no quotes), any
    other value prints as its JSON scalar (`true`, a number). A path that does
    not resolve yields "" rather than jq's literal `null`, so the callers that
    test the result for emptiness see "nothing" instead of a four-letter word
    that could be mistaken for a value.
    """
    program = (program or "").strip()
    if not _JQ_FIELD_PATH_RE.fullmatch(program):
        raise PostError(f"gh not installed, and --jq {program!r} is not a "
                        "plain field path the REST fallback can evaluate")
    try:
        value = json.loads(out or "null")
    except json.JSONDecodeError as exc:
        raise PostError(f"GitHub returned unparseable JSON: {exc}") from None
    for key in program.lstrip(".").split("."):
        if not isinstance(value, dict):
            return ""
        value = value.get(key)
    if value is None:
        return ""
    return value if isinstance(value, str) else json.dumps(value)


def find_pr(info: RepoInfo) -> int | None:
    """Return the number of the open PR whose head is info.branch, else None.

    GitHub allows SEVERAL open PRs from one head branch, as long as their bases
    differ — which is exactly what stacked work produces (child -> parent while
    parent is in flight, then child -> main once it lands). Taking whichever
    row `gh` happened to list first meant the card could hop between those PRs
    from one push to the next, and every push would fight the previous one.
    _choose_pr makes the choice deterministic and logs the ambiguity; `--pr N`
    settles it outright.
    """
    # REST equivalent for gh-less hosts: full PR objects rather than gh's
    # --json projection. _choose_pr reads both shapes, so the choice above is
    # made the same way whether gh answered or the REST fallback did.
    head = urllib.parse.quote(f"{info.owner}:{info.branch}", safe="")
    out = _run_gh(
        ["pr", "list", "--head", info.branch,
         "--json", "number,baseRefName", "--state", "open"],
        cwd=info.root,
        timeout=_GH_FOREGROUND_TIMEOUT,
        rest=("GET", f"repos/{info.owner}/{info.repo}/pulls"
                     f"?head={head}&state=open"),
    )
    try:
        rows = json.loads(out or "[]")
    except json.JSONDecodeError as exc:
        raise PostError(f"gh pr list returned unparseable JSON: {exc}") from None
    if not isinstance(rows, list):
        raise PostError(f"gh pr list returned {type(rows).__name__}, not a list")
    return _choose_pr(rows, info)


def _choose_pr(rows: list, info: RepoInfo) -> int | None:
    """The one PR to review, chosen the same way on every run.

    Preference order: the PR whose base is the branch this review is already
    against, then the recorded parent, then the default branch — so the diff
    Crux computes and the diff GitHub shows agree without a re-base. Ties (and
    a set of bases matching none of those) fall to the OLDEST PR, which is
    stable as new ones are opened.
    """
    candidates: list[tuple[int, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        # `gh pr list --json` calls it baseRefName; the gh-less REST fallback
        # returns whole PR objects, where the same fact is base.ref. Read
        # either, so a stacked PR is chosen identically on both paths.
        base = row.get("base")
        base_ref = row.get("baseRefName") or (
            base.get("ref") if isinstance(base, dict) else "")
        try:
            candidates.append((int(row["number"]), str(base_ref or "")))
        except (KeyError, TypeError, ValueError):
            continue  # a row we cannot read is a row we cannot choose
    if not candidates:
        return None
    candidates.sort()  # oldest first: both the tiebreak and a stable scan order
    if len(candidates) == 1:
        return candidates[0][0]

    chosen = candidates[0][0]
    for base in (info.base_branch, info.crux_base, info.default_branch):
        if not base:
            continue
        match = next((n for n, row_base in candidates if row_base == base), None)
        if match is not None:
            chosen = match
            break
    log.warning(
        "%s has %d open PRs (%s); reviewing #%d — pass `--pr N` to review "
        "another", info.branch, len(candidates),
        ", ".join(f"#{n} -> {b or '?'}" for n, b in candidates), chosen,
    )
    return chosen


def pr_base(info: RepoInfo, pr: int) -> str | None:
    """The branch PR *pr* actually merges into, or None if it can't be read.

    This is the D34 authority for the review's diff base. The local guess
    (cruxBase / reflog / default branch) is right most of the time and wrong in
    exactly the cases that hurt: a PR retargeted on GitHub, one opened by hand
    into another branch, and one GitHub auto-retargeted when its base was
    merged and deleted. Best-effort — a failed lookup leaves the guess in
    place rather than failing the run.
    """
    try:
        out = _run_gh(["api", f"repos/{info.owner}/{info.repo}/pulls/{pr}",
                       "--jq", ".base.ref"],
                      cwd=info.root, timeout=_GH_FOREGROUND_TIMEOUT)
    except PostError as exc:
        log.warning("could not read the base branch of PR #%d: %s", pr, exc)
        return None
    base = (out or "").strip()
    return base or None


def pr_meta(info: RepoInfo, pr: int) -> tuple[str, str]:
    """(created_at, author display name) for the Slack line — ONE gh call.

    Both facts feed the same announcement, so they are fetched together rather
    than costing a round trip each.

    `gh pr view` rather than the REST pulls endpoint: REST returns a *simple
    user* object whose only identity field is `login`, so the real name a
    human recognises is reachable only through the GraphQL-backed command
    (verified — REST has no `user.name`). The login is still the fallback for
    accounts with no display name set — and it is all the gh-less REST
    fallback below can offer, which is why that path is the fallback and not
    the default.

    Best-effort, never raises: a failed lookup costs the credit, never the
    announcement.
    """
    try:
        out = _run_gh(["pr", "view", str(pr), "--repo",
                       f"{info.owner}/{info.repo}", "--json", "author,createdAt"],
                      cwd=info.root, timeout=_GH_FOREGROUND_TIMEOUT,
                      # gh-less hosts: the PR object carries both facts, just
                      # under REST's names (created_at / user) — read below.
                      rest=("GET",
                            f"repos/{info.owner}/{info.repo}/pulls/{pr}"))
        data = json.loads(out or "{}")
        author = data.get("author") or data.get("user") or {}
        return (str(data.get("createdAt") or data.get("created_at") or ""),
                str(author.get("name") or author.get("login") or ""))
    except (PostError, ValueError, AttributeError):
        return "", ""


def author_name(info: RepoInfo) -> str:
    """Who to credit on the announcement, read from git — no network call.

    `git config user.name` is the identity that authored the commits being
    announced, which is the normal case for a review Crux posts right after
    your own push. Falls back to the branch tip's author when the config is
    unset. Best-effort: an empty result costs a credit, never the message.
    """
    from crux.gitio import _try_git
    return (_try_git(["config", "user.name"], cwd=info.root)
            or _try_git(["log", "-1", "--format=%an"], cwd=info.root)
            or "")


def default_base(info: RepoInfo, cfg: Config) -> str:
    """The branch a new PR targets: the branch this branch was created from
    (crux_base), else the configured default. main is only a last resort."""
    return info.crux_base or cfg.pr_default_base


def ensure_pr(info: RepoInfo, cfg: Config, interactive: bool = True) -> int | None:
    """Return an existing PR number, or (D11) create one against its base.

    The base is crux_base (the branch this branch was created from) when known,
    else the configured default. PR creation is a convenience; the review card
    is posted only once a PR exists (the caller gates on the return value).

    interactive=True (a human ran crux after the push): with pr_auto_create,
    create straight away; otherwise ask on /dev/tty. Answer semantics: Y/enter
    => create into the default base; n => don't create; any other text => use
    that text as the base.

    interactive=False (the detached `crux run --yes` spawned by the pre-push
    hook): never prompt. Honor a base recorded by the foreground pre-push ask
    (record_pr_intent); with none, create into the default base iff
    pr_auto_create, else return None. The push has landed by now, so
    `gh pr create` works.
    """
    number = find_pr(info)
    if number is not None:
        # The answer to "should I open a PR?" is moot once one exists. Drop it
        # rather than leaving it to fire against a later, unrelated push.
        consume_intent(info)
        return number
    if not interactive:
        intent = consume_intent(info)
        base = intent.get("base")
        if base is None and cfg.pr_auto_create:
            base = default_base(info, cfg)
        if base is None:
            log.info("no open PR for %s and no recorded pre-push answer; "
                     "skipping create (D11)", info.branch)
            return None
        # No prompting from the detached run: it has no terminal of its own,
        # so only the answer recorded at pre-push time (or push_head =
        # "always") may push the head branch.
        _ensure_head_on_remote(info, cfg, agreed=intent.get("push_head"),
                               may_ask=False)
        return _create_pr(info, base, cfg)
    # Interactive: a human is here, so the head question can be asked now —
    # but a pre-push answer, if one was recorded, is the same person's and
    # stands. Read without consuming: the intent belongs to the push, and the
    # detached run is the one that consumes it.
    recorded = _load_intent(info).get("push_head")
    if cfg.pr_auto_create:
        _ensure_head_on_remote(info, cfg, agreed=recorded, may_ask=True)
        return _create_pr(info, default_base(info, cfg), cfg)
    base = _ask_base(info, cfg)
    if base is None:
        return None
    _ensure_head_on_remote(info, cfg, agreed=recorded, may_ask=True)
    return _create_pr(info, base, cfg)


def _ask_base(info: RepoInfo, cfg: Config) -> str | None:
    """The D11 tty ask. Returns the chosen base branch, or None to not create."""
    base = default_base(info, cfg)
    answer = _prompt_tty(
        f"Create a PR for {info.branch} into {base}? [Y/n/branch-name] "
    )
    if answer is None:
        log.info("no /dev/tty available; skipping PR create for %s", info.branch)
        return None
    answer = answer.strip()
    if answer.lower() in ("n", "no"):
        return None
    if answer == "" or answer.lower() in ("y", "yes"):
        return base
    return answer


# ---------------------------------------------------------------------------
# D11 pre-push intent: the foreground hook records the answer, the detached
# run consumes it AFTER the push lands (gh pr create fails before the head
# branch exists on the remote, so creation must never happen at pre-push time).
# ---------------------------------------------------------------------------

def _pr_intent_path(info: RepoInfo) -> Path:
    safe_branch = info.branch.replace("/", "__")
    return (Path.home() / ".cache" / "crux" / f"{info.owner}__{info.repo}"
            / f"{safe_branch}.pr-intent.json")


def _store_intent(info: RepoInfo, **answers: object) -> None:
    """Merge *answers* into this branch's intent file, stamped with HEAD.

    Merged rather than overwritten because the pre-push foreground asks more
    than one question (the D11 base, the D39 head push) and each records its
    own answer as it is given.
    """
    path = _pr_intent_path(info)
    data = _load_intent(info)
    data.update(answers)
    data["head_sha"] = info.head_sha
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
    except OSError as exc:
        raise PostError(f"could not record PR intent: {exc}") from None


def _load_intent(info: RepoInfo) -> dict:
    """The recorded answers, without consuming them. {} when there are none."""
    try:
        data = json.loads(_pr_intent_path(info).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def record_pr_intent(info: RepoInfo, cfg: Config) -> str | None:
    """Foreground half of the D11 pre-push ask: record the answer, create nothing.

    Returns the recorded base branch, or None when the user declined or no
    tty was available.
    """
    base = _ask_base(info, cfg)
    if base is None:
        return None
    _store_intent(info, base=base)
    return base


def record_push_head_intent(info: RepoInfo, cfg: Config) -> bool:
    """D39 pre-push ask: may Crux push the head branch after this push lands?

    Only asked when the branch tracks a DIFFERENT remote branch — the rename
    signature, decidable without the network. In every other case the push
    about to happen either creates the matching remote branch or updates it,
    so there is nothing to ask about and nothing is recorded.

    Returns whether the head will be pushed.
    """
    if cfg.pr_push_head == "never":
        return False
    tracked = tracked_branch_mismatch(info)
    if tracked is None:
        return False
    if cfg.pr_push_head == "always":
        _store_intent(info, push_head=True)
        return True
    agreed = _ask_push_head(info, cfg, tracked)
    _store_intent(info, push_head=agreed)
    return agreed


def consume_intent(info: RepoInfo) -> dict:
    """Read and delete the recorded pre-push answers; {} when there are none.

    The answers are only honored for the push they were recorded for: the
    recorded head must be an ancestor of (or equal to) HEAD. It normally IS
    HEAD — the detached run starts seconds later — and staying
    ancestor-tolerant keeps a commit made during that window from throwing the
    answers away. What it does reject is an intent stranded by a run that never
    happened and then applied, branch state later, to work the user never
    answered for. Either way the file is consumed exactly once.
    """
    path = _pr_intent_path(info)
    data = _load_intent(info)
    try:
        path.unlink()
    except OSError:
        pass
    if not data:
        return {}
    recorded_head = str(data.get("head_sha") or "")
    if recorded_head and not _is_ancestor(info, recorded_head):
        log.info("discarding the pre-push PR answer for %s: it was recorded "
                 "for %s, which this branch no longer contains",
                 info.branch, recorded_head[:12])
        return {}
    return data


def consume_pr_intent(info: RepoInfo) -> str | None:
    """The D11 base answer alone. Consumes the whole intent file (once)."""
    base = consume_intent(info).get("base")
    return str(base) if base else None


def _is_ancestor(info: RepoInfo, sha: str) -> bool:
    """True when *sha* is reachable from HEAD. False on any git trouble —
    an unreadable repo must not silently validate stale state."""
    try:
        proc = subprocess.run(
            ["git", "merge-base", "--is-ancestor", sha, info.head_sha],
            capture_output=True, encoding="utf-8", errors="replace",
            cwd=info.root, timeout=_GH_FOREGROUND_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def set_status(info: RepoInfo, pr: int | None, state: str, description: str) -> None:
    """Best-effort GitHub commit status so the PR/commit shows Crux's state.

    Renders as a check on the PR: a pending ● while the review runs, then a
    green ✓ (or red ✗) when it finishes. Never raises — a missing status must
    not fail or block the run; it is only a progress indicator.

    state is one of: pending | success | failure | error.
    """
    payload: dict = {
        "state": state,
        "context": "Crux review",
        "description": description[:140],
    }
    if pr is not None:
        payload["target_url"] = (
            f"https://github.com/{info.owner}/{info.repo}/pull/{pr}")
    try:
        _run_gh(
            ["api", "-X", "POST",
             f"repos/{info.owner}/{info.repo}/statuses/{info.head_sha}",
             "--input", "-"],
            cwd=info.root,
            stdin_text=json.dumps(payload),
        )
    except PostError as exc:
        log.warning("could not set commit status (%s): %s", state, exc)


def upsert_comment(info: RepoInfo, pr: int, body: str,
                   marker: str = CARD_MARKER) -> None:
    """Create or edit-in-place a single sticky Crux comment on the PR (D8).

    The sticky comment is found by scanning all issue comments (paginated) for
    *marker* in the body; found => PATCH, else POST. *marker* selects which
    sticky comment (the review card, or the integration-test comment).
    """
    comments_path = f"repos/{info.owner}/{info.repo}/issues/{pr}/comments"
    out = _run_gh(["api", comments_path, "--paginate"], cwd=info.root)
    existing_id = _find_marker_comment_id(out, marker)
    payload = json.dumps({"body": body})
    if existing_id is not None:
        _run_gh(
            ["api", "-X", "PATCH",
             f"repos/{info.owner}/{info.repo}/issues/comments/{existing_id}",
             "--input", "-"],
            cwd=info.root,
            stdin_text=payload,
        )
    else:
        _run_gh(
            ["api", "-X", "POST", comments_path, "--input", "-"],
            cwd=info.root,
            stdin_text=payload,
        )


# Env var carrying an inherited, already-open write fd to the controlling
# terminal, handed from a hook down to its detached background run (see
# open_terminal_fd / _spawn_detached). Once a run detaches (setsid severs its
# controlling terminal) it can no longer open /dev/tty by name — but a fd
# opened *before* the sever still writes to that terminal, so we pass the fd.
_TTY_FD_ENV = "CRUX_TTY_FD"


def _tty_devices() -> tuple[str, str]:
    """(write_device, read_device) for the controlling terminal on this OS.

    POSIX exposes it as /dev/tty; the Windows console is reached through the
    magic CONOUT$/CONIN$ device names. Opening either fails cleanly (OSError)
    when there is no console — a detached run, CI, an editor, an agent — so
    callers degrade to a no-op rather than crash.
    """
    if os.name == "nt":
        return "CONOUT$", "CONIN$"
    return "/dev/tty", "/dev/tty"


def tty_available() -> bool:
    """Whether a controlling terminal can be opened for a QUESTION.

    Probed before any expensive work a question would gate — D40 fetches the
    Zenhub ticket list to ask about, and a hook with no terminal (CI, an IDE,
    an agent, a detached run) must not pay a network round trip for a question
    it could never ask. Opens the same two devices `_prompt_tty` does, so it
    answers the question actually being asked rather than a proxy for it.
    """
    write_dev, read_dev = _tty_devices()
    try:
        with open(write_dev, "w", encoding="utf-8"), \
             open(read_dev, "r", encoding="utf-8"):
            return True
    except OSError:
        return False


def open_terminal_fd() -> int | None:
    """A writable fd to the controlling terminal, or None if there is none.

    A hook opens this while it still has the terminal and hands it to its
    detached background run (CRUX_TTY_FD + Popen pass_fds). Detaching severs the
    child's controlling terminal, so it can no longer open /dev/tty by name —
    but this fd, opened before the sever, keeps pointing at that terminal, so
    the run's notices still reach the user (and writes just fail cleanly once
    the terminal closes). POSIX only: on Windows a console handle can't be
    inherited across a DETACHED_PROCESS, so background notices stay quiet there
    (as they did before) and this returns None.
    """
    if os.name == "nt":
        return None
    write_dev, _ = _tty_devices()
    try:
        # O_NOCTTY: write to the terminal, never (re)acquire it as ours.
        return os.open(write_dev, os.O_WRONLY | os.O_NOCTTY)
    except OSError:
        return None  # no controlling terminal (CI, editor, agent, detached)


def notify_tty(message: str) -> None:
    """Best-effort one-line note to the controlling terminal.

    Used so a push from a real terminal shows that crux is doing something
    (the pre-push hook routes crux's stdout/stderr to the log file, so a plain
    print would never reach the user). Reaches the terminal even from the
    detached background run, which can't open /dev/tty by name: it writes to the
    inherited CRUX_TTY_FD the hook handed down (see open_terminal_fd). No-op
    when there is no usable tty.
    """
    fd_env = os.environ.get(_TTY_FD_ENV)
    if fd_env:
        try:
            os.write(int(fd_env), (message + "\n").encode("utf-8", "replace"))
            return
        except (OSError, ValueError):
            pass  # stale/closed fd — fall back to opening the tty by name
    write_dev, _ = _tty_devices()
    try:
        with open(write_dev, "w", encoding="utf-8", errors="replace") as out:
            out.write(message + "\n")
            out.flush()
    except OSError:
        pass


def progress_card(info: RepoInfo) -> str:
    """Sticky-comment body posted the moment analysis starts, so the PR shows
    that a review is coming. Carries CARD_MARKER so the finished card replaces
    it in place via upsert_comment."""
    # base_branch, not crux_base: what the diff is actually against (the PR's
    # own base once aligned, D34), not where the branch happened to fork from.
    base = info.base_branch or info.crux_base or info.default_branch
    return (f"{CARD_MARKER}\n"
            f"## 🔍 Crux is reviewing `{info.head_sha[:7]}`…\n\n"
            f"Analyzing this branch's changes against `{base}`. "
            f"This comment will update with the review in a moment.")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _prompt_tty(message: str) -> str | None:
    """Ask on the controlling terminal; None if there is no usable tty.

    Crux typically runs from a git hook where stdin/stdout are redirected, so
    the D11 prompt must go to the terminal device directly (/dev/tty on POSIX,
    CONOUT$/CONIN$ on Windows). Open two SEPARATE handles — a write-only one
    for the question, a read-only one for the answer — not a single "r+"
    handle: a buffered read+write stream on a tty is not seekable, and
    interleaving a write with a read on it raises io.UnsupportedOperation
    ("File or stream is not seekable") on some Python/libc builds (e.g.
    Fedora). That is an OSError subclass, so the old code caught it and wrongly
    reported "no tty available" on a terminal that was in fact fine.
    """
    write_dev, read_dev = _tty_devices()
    try:
        with open(write_dev, "w", encoding="utf-8", errors="replace") as out, \
             open(read_dev, "r", encoding="utf-8", errors="replace") as inp:
            out.write(message)
            out.flush()
            line = inp.readline()
    except OSError:
        return None
    if line == "":  # EOF (e.g. tty closed under us)
        return None
    return line.strip()


# A definite "this branch is gone" from the GitHub API. Anything else (401,
# 5xx, a timeout, no network) says nothing about whether the branch exists.
_NOT_FOUND_RE = re.compile(r"HTTP 404|Not Found", re.IGNORECASE)


def _remote_branch_missing(info: RepoInfo, branch: str) -> bool:
    """True only when the remote answers a definite 404 for *branch*.

    Any other gh failure — expired auth, a network blip, a timeout — proves
    nothing, and must NOT be read as "the branch was deleted": that would
    silently retarget a stacked PR onto main, which is far worse than letting
    `gh pr create` fail loudly (D34).
    """
    try:
        _run_gh(
            ["api", f"repos/{info.owner}/{info.repo}/branches/{branch}"],
            cwd=info.root,
            timeout=_GH_FOREGROUND_TIMEOUT,
        )
    except PostError as exc:
        if _NOT_FOUND_RE.search(str(exc)):
            return True
        log.warning("could not check whether %r still exists on the remote "
                    "(%s); keeping it as the base", branch, exc)
        return False
    return False


def _resolve_base(info: RepoInfo, cfg: Config, base: str) -> str:
    """Make sure the PR base is a branch that still exists on the remote.

    A recorded crux_base can name a branch that was merged and deleted since the
    intent was recorded (the design-branch case): `gh pr create` then dies with
    "Base ref must be a branch" / "No commits between ...", which aborts the whole
    crux run. When the intended base is gone, fall back to the configured default
    (main) so the PR is still created, and log the substitution. If nothing
    better exists, return *base* unchanged and let gh surface the real error.

    Only a definite 404 counts as gone (D34) — a substitution made on a network
    blip would open a stacked PR against the wrong branch.
    """
    if not _remote_branch_missing(info, base):
        return base
    fallback = cfg.pr_default_base
    if fallback != base and not _remote_branch_missing(info, fallback):
        log.warning(
            "PR base %r no longer exists on the remote; creating against %r "
            "instead", base, fallback,
        )
        return fallback
    return base


# ---------------------------------------------------------------------------
# D39 the missing head branch. `git branch -m` renames the local branch but
# leaves branch.<name>.merge pointing at the name it had, so the next push
# moves the OLD remote branch and the new name never reaches the remote.
# `gh pr create --head <new-name>` then fails with "Head ref must be a branch"
# / "No commits between ..." and the whole run dies. Push the head instead —
# with the user's agreement, since a push is not Crux's to make silently.
# ---------------------------------------------------------------------------

def tracked_branch_mismatch(info: RepoInfo) -> str | None:
    """The remote branch this branch tracks, when it is NOT its own name.

    That is the rename signature, and it needs no network: a push will move
    the tracked branch, not create one called `info.branch`. None when the
    branch tracks nothing (a plain push creates the matching remote branch)
    or already tracks its own name.

    Read from branch.<name>.remote/.merge rather than parsing @{u}: a remote
    name may itself contain a slash, so "origin/feat/x" cannot be split back
    into remote and branch unambiguously.
    """
    from crux.gitio import _try_git
    remote = _try_git(["config", f"branch.{info.branch}.remote"], cwd=info.root)
    merge = _try_git(["config", f"branch.{info.branch}.merge"], cwd=info.root)
    if not remote or not merge:
        return None
    tracked = merge.strip().removeprefix("refs/heads/")
    return tracked if tracked and tracked != info.branch else None


def _ask_push_head(info: RepoInfo, cfg: Config, tracked: str | None) -> bool:
    """Ask on the tty whether to push the head branch. False with no tty."""
    remote = cfg.pr_push_remote
    why = (f"{info.branch} tracks {remote}/{tracked}, so the push updates that "
           f"branch and never creates {info.branch}"
           if tracked else
           f"{info.branch} does not exist on {remote}")
    answer = _prompt_tty(f"{why}; a PR needs it as the head. "
                         f"Push {info.branch} to {remote}? [Y/n] ")
    if answer is None:
        log.info("no /dev/tty available; not pushing %s to %s",
                 info.branch, remote)
        return False
    return answer.strip().lower() in ("", "y", "yes")


def push_head(info: RepoInfo, cfg: Config) -> bool:
    """`git push -u <remote> <branch>`; True when the branch is now on the remote.

    The -u is the point of the fix as much as the push is: it repoints the
    upstream at the branch's own name, so the next push needs no rescue.

    Never raises. A failed push leaves `gh pr create` to fail with its own,
    accurate error rather than one invented here.
    """
    remote = cfg.pr_push_remote
    try:
        proc = subprocess.run(
            ["git", "push", "-u", remote, info.branch],
            capture_output=True, encoding="utf-8", errors="replace",
            cwd=info.root, timeout=_GH_BACKGROUND_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.error("could not push %s to %s: %s", info.branch, remote, exc)
        return False
    if proc.returncode != 0:
        log.error("git push %s %s failed: %s", remote, info.branch,
                  (proc.stderr or "").strip()[:_STDERR_EXCERPT_CHARS])
        return False
    log.info("pushed %s to %s so the PR has a head (D39)", info.branch, remote)
    return True


def _ensure_head_on_remote(info: RepoInfo, cfg: Config, *,
                           agreed: object = None, may_ask: bool = False) -> None:
    """Push the head branch when it is missing from the remote and allowed.

    *agreed* is the answer recorded at pre-push time (None = none recorded).
    *may_ask* is for callers with a terminal: the detached run has none, so it
    passes False and acts only on what was already agreed.

    The gate is a definite 404 on the head (D34) — never a network blip, which
    would push a branch nobody asked about.
    """
    if cfg.pr_push_head == "never" or agreed is False:
        return
    if not _remote_branch_missing(info, info.branch):
        return
    if cfg.pr_push_head != "always" and agreed is not True:
        if not may_ask or not _ask_push_head(info, cfg,
                                             tracked_branch_mismatch(info)):
            return
    push_head(info, cfg)


def _create_pr(info: RepoInfo, base: str, cfg: Config) -> int:
    base = _resolve_base(info, cfg, base)
    title = pr_title_from_commits(info)
    body = pr_body_from_commits(info)
    out = _run_gh(
        ["pr", "create",
         "--base", base,
         "--head", info.branch,
         "--title", title,
         "--body", body],
        cwd=info.root,
        # gh-less hosts: same create via REST; the response JSON carries the
        # new PR's html_url, so the /pull/N scrape below works on both paths.
        rest=("POST", f"repos/{info.owner}/{info.repo}/pulls",
              {"title": title, "head": info.branch, "base": base, "body": body}),
    )
    # gh pr create prints the new PR's URL, e.g. https://github.com/o/r/pull/42
    match = re.search(r"/pull/(\d+)", out)
    if match is None:
        raise PostError(
            f"could not parse PR number from gh pr create output: {out.strip()[:200]!r}"
        )
    return int(match.group(1))


# Cap the commit list so a long-lived branch doesn't produce a giant body.
_PR_BODY_MAX_COMMITS = 50
# Hidden marker distinguishing a Crux commit-list body from a review-written one
# (below). D26: the marker no longer gates the sync — Crux keeps the title +
# description its own even after a human edit; the only opt-out is PR_KEEP_TOKEN.
_PR_BODY_MARKER = "<!-- crux:pr-body -->"
# Variant marker for a description written from the LLM review (summary +
# overview). The pre-review commit-list sync never overwrites a body carrying
# this marker — the same D19 rule that keeps an LLM pr_title from being
# downgraded back to the commit-based placeholder.
_PR_BODY_LLM_MARKER = "<!-- crux:pr-body:llm -->"
# D26 opt-out: a human adds this HTML comment to the PR description and Crux
# stops touching the title AND the description. It must be the COMMENT form —
# the bare word appearing in a commit subject or the review prose is NOT an
# opt-out, so Crux never locks itself out with its own auto-generated text.
PR_KEEP_TOKEN = "crux:keep"
_PR_KEEP_RE = re.compile(r"<!--\s*crux:keep\s*-->", re.IGNORECASE)
# Appended to every Crux-written body so the opt-out is discoverable in place —
# every viewer of the PR sees how to keep their own text. The comment is shown
# INSIDE backticks so it renders visibly (a bare HTML comment is invisible);
# _human_opted_out strips this exact hint before matching, so Crux's own hint
# never reads as an opt-out. D32: this ONE small-print line is the whole
# footer — it already says Crux wrote the body, so the separate "Description
# written by Crux…" line it used to sit under was pure repetition.
_PR_KEEP_HINT = (
    f"<sub>🤖 Written by Crux, which keeps this PR's title and description in "
    f"sync with the branch. Add `<!-- {PR_KEEP_TOKEN} -->` to keep your own.</sub>"
)


def _human_opted_out(body: str) -> bool:
    """True when a human placed the D26 opt-out comment in the description.

    Only the ``<!-- crux:keep -->`` comment form counts, and Crux's own hint
    (which displays that comment) is stripped first — so neither the hint, a
    commit subject mentioning the token, nor the review prose can make Crux
    stop syncing. Only a deliberate human opt-out does.
    """
    return bool(_PR_KEEP_RE.search(body.replace(_PR_KEEP_HINT, "")))


# Branch leaf names too generic to make a good PR title on their own.
_GENERIC_BRANCH_LEAVES = {
    "main", "master", "dev", "develop", "trunk", "release", "hotfix",
    "wip", "temp", "tmp", "patch", "fix", "test", "feature", "bugfix", "chore",
}


def pr_title_from_commits(info: RepoInfo) -> str:
    """A quick, LLM-free PR title.

    One commit -> its subject (the clearest signal). Several commits -> the
    branch name humanized ('add-write-buffer' -> 'Add write buffer'), which
    describes the whole branch better than any single commit and stays stable
    as commits are added. Falls back to the newest commit subject when the
    branch name is generic or uninformative, then to the branch itself.
    """
    subjects = _commit_subjects(info)  # newest-first
    if len(subjects) == 1:
        return subjects[0]
    humanized = _humanize_branch(info.branch)
    if humanized:
        return humanized
    return subjects[0] if subjects else info.branch


def _humanize_branch(branch: str) -> str:
    """'feat/123-add-write-buffer' -> 'Add write buffer'; '' if uninformative
    (a generic leaf like 'main'/'wip', or only issue numbers)."""
    leaf = branch.rsplit("/", 1)[-1]  # drop feat/, jr/, users/x/ … prefixes
    words = [w for w in re.split(r"[-_]+", leaf) if w and not w.isdigit()]
    if not words or all(w.lower() in _GENERIC_BRANCH_LEAVES for w in words):
        return ""
    text = " ".join(words)
    return text[0].upper() + text[1:]


def pr_body_from_commits(info: RepoInfo, review_url: str | None = None,
                         test_url: str | None = None) -> str:
    """A quick, LLM-free PR description built from this branch's commits.

    Lists the commit subjects between the base (crux_base fork point) and HEAD,
    newest first, and — when they exist — links to the Crux review comment and
    the how-to-test comment. Falls back to a plain note if git is unavailable.
    """
    parts = [_PR_BODY_MARKER]
    subjects = _commit_subjects(info)
    if not subjects:
        parts.append("_Opened by Crux._")
    else:
        shown = subjects[:_PR_BODY_MAX_COMMITS]
        lines = "\n".join(f"- {s}" for s in shown)
        more = len(subjects) - len(shown)
        if more > 0:
            lines += f"\n- …and {more} more commit(s)"
        parts.append(f"## Summary\n\n{lines}")
    links = []
    if review_url:
        links.append(f"- 📋 [Crux review]({review_url})")
    if test_url:
        links.append(f"- 🧪 [How to test this]({test_url})")
    if links:
        parts.append("\n".join(links))
    parts.append(_PR_KEEP_HINT)
    return "\n\n".join(parts)


def pr_body_from_annotation(summary: str, overview: list[str],
                            review_url: str | None = None,
                            test_url: str | None = None,
                            info: RepoInfo | None = None) -> str:
    """The PR description written from the review itself (Annotation.summary +
    overview): a curated account of what the branch does and what matters,
    replacing the raw commit list — trivial commits never appear. Returns ""
    when the annotation carries no usable text (caller falls back to commits).

    With *info*, the `path/file.py:line` pointer ending each bullet becomes a
    link to that code, as it already is on the card (D32) — unlinked, it is
    clutter the reader has to resolve by hand.
    """
    summary = (summary or "").strip()
    bullets = [b.strip() for b in overview or [] if b and b.strip()]
    if not summary and not bullets:
        return ""
    if info is not None:
        from crux.render import linkify
        summary = linkify(info, summary)
        bullets = [linkify(info, b) for b in bullets]
    section = "## Summary"
    if summary:
        section += f"\n\n{summary}"
    if bullets:
        section += "\n\n" + "\n".join(f"- {b}" for b in bullets)
    parts = [_PR_BODY_LLM_MARKER, section]
    links = []
    if review_url:
        links.append(f"- 📋 [Crux review]({review_url})")
    if test_url:
        links.append(f"- 🧪 [How to test this]({test_url})")
    if links:
        parts.append("\n".join(links))
    parts.append(_PR_KEEP_HINT)
    return "\n\n".join(parts)


def find_comment_urls(info: RepoInfo, pr: int, markers: list[str]) -> dict:
    """Map each sticky-comment marker to the html_url of its comment (or None).
    One paginated fetch; best-effort (all None on any error)."""
    urls: dict = {m: None for m in markers}
    try:
        out = _run_gh(
            ["api", f"repos/{info.owner}/{info.repo}/issues/{pr}/comments",
             "--paginate"], cwd=info.root)
        for comment in _iter_paginated(out):
            body = comment.get("body") or ""
            for marker in markers:
                if urls[marker] is None and marker in body:
                    urls[marker] = comment.get("html_url")
    except (PostError, ValueError):
        pass
    return urls


def sync_pr_metadata(info: RepoInfo, pr: int, title: str | None = None,
                     summary: str = "", overview: list[str] | None = None) -> None:
    """Keep the PR title + description in step with the branch.

    *title*: when given, set the PR title to it (the LLM-written pr_title after a
    review). When None, leave the title ALONE and refresh only the description —
    so the pre-review description sync never downgrades a good LLM title back to
    the commit-based placeholder.

    *summary*/*overview*: when the review's annotation is given, the description
    is rewritten from it — what the branch does and what matters, with trivial
    commits curated out. Without it (pre-review, --no-llm), the description is
    the fast commit list — unless the current body is already review-written
    (LLM marker), which is never downgraded back to a commit list.

    D26: human edits do NOT stop the sync — Crux keeps the title + description
    its own on every push, even over a human-written one. The one escape hatch
    is explicit: a human adds the ``<!-- crux:keep -->`` comment to the
    description and Crux leaves BOTH the title and the body alone. Crux's own
    auto-generated body (which carries a discoverability hint naming that
    comment) never counts as an opt-out, so Crux cannot lock itself out.

    PATCHes only when something changed. Best-effort: never raises.
    """
    try:
        out = _run_gh(["api", f"repos/{info.owner}/{info.repo}/pulls/{pr}"],
                      cwd=info.root)
        current = json.loads(out or "{}")
    except (PostError, ValueError) as exc:
        log.warning("could not read PR #%d for metadata sync: %s", pr, exc)
        return
    cur_body = current.get("body") or ""
    if _human_opted_out(cur_body):
        log.info("PR #%d description carries the crux:keep opt-out; leaving "
                 "title + body alone (D26)", pr)
        return

    urls = find_comment_urls(info, pr, [CARD_MARKER, TEST_MARKER])
    body = pr_body_from_annotation(summary, overview or [],
                                   urls[CARD_MARKER], urls[TEST_MARKER],
                                   info=info)
    source = "the review"
    if not body:
        if _PR_BODY_LLM_MARKER in cur_body:
            body = cur_body  # never downgrade a review-written description
        else:
            body = pr_body_from_commits(info, urls[CARD_MARKER],
                                        urls[TEST_MARKER])
            source = "your commits"
    payload: dict = {}
    # Only touch the title when the caller supplies one (the LLM pr_title).
    if title and title.strip() and title.strip() != current.get("title"):
        payload["title"] = title.strip()
    if body != cur_body:
        payload["body"] = body
    if not payload:
        return
    try:
        _run_gh(
            ["api", "-X", "PATCH", f"repos/{info.owner}/{info.repo}/pulls/{pr}",
             "--input", "-"],
            cwd=info.root,
            stdin_text=json.dumps(payload),
        )
        notify_tty(f"📝 Crux: refreshed the PR title & description from {source} "
                   "(add <!-- crux:keep --> to the description to keep your own)")
        log.info("synced PR #%d %s from %s", pr, "+".join(payload), source)
    except PostError as exc:
        log.warning("could not sync PR #%d metadata: %s", pr, exc)


def _commit_subjects(info: RepoInfo) -> list[str]:
    """Effective commit subjects unique to this branch (base_sha..head_sha),
    newest first. D27: for a commit Crux amended, the Crux-written subject is
    used instead of the terse human line above it — full messages are read
    (NUL-separated), never just %s."""
    from crux.commitmsg import effective_subject
    try:
        proc = subprocess.run(
            ["git", "log", "--format=%B%x00", f"{info.base_sha}..{info.head_sha}"],
            capture_output=True,
            encoding="utf-8", errors="replace",
            cwd=info.root,
            timeout=_GH_FOREGROUND_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    subjects = [effective_subject(m) for m in (proc.stdout or "").split("\x00")]
    return [s for s in subjects if s]


def _last_commit_subject(info: RepoInfo) -> str:
    """Effective subject of HEAD (D27-aware), used as the PR title; branch
    name if git fails."""
    from crux.commitmsg import effective_subject
    try:
        proc = subprocess.run(
            ["git", "log", "-1", "--format=%B"],
            capture_output=True,
            encoding="utf-8", errors="replace",
            cwd=info.root,
            timeout=_GH_FOREGROUND_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return info.branch
    subject = effective_subject(proc.stdout or "")
    if proc.returncode != 0 or not subject:
        return info.branch
    return subject


def _find_marker_comment_id(paginated_json: str,
                            marker: str = CARD_MARKER) -> int | None:
    for comment in _iter_paginated(paginated_json):
        if marker in (comment.get("body") or ""):
            return int(comment["id"])
    return None


def _iter_paginated(text: str) -> Iterator[dict]:
    """Yield comment objects from `gh api --paginate` output.

    --paginate emits one JSON document per page concatenated back-to-back
    ("[...][...]"), so a single json.loads fails on multi-page output;
    raw_decode in a loop handles any page count.
    """
    decoder = json.JSONDecoder()
    idx, end = 0, len(text)
    while idx < end:
        while idx < end and text[idx].isspace():
            idx += 1
        if idx >= end:
            break
        try:
            doc, idx = decoder.raw_decode(text, idx)
        except json.JSONDecodeError as exc:
            raise PostError(f"gh api returned unparseable JSON: {exc}") from None
        if isinstance(doc, list):
            yield from (c for c in doc if isinstance(c, dict))
        elif isinstance(doc, dict):
            yield doc
