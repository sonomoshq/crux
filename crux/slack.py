# Copyright © 2026 Sonomos, Inc.
# SPDX-License-Identifier: Apache-2.0

"""Slack integration (D16): announce PRs to a channel, thread later updates.

Auth: a bot token resolved by ``_token`` — the env var named by
``cfg.slack_token_env`` (default ``SLACK_BOT_TOKEN``), else the credentials
file (``~/.config/crux/credentials.json``, shell-independent). Scopes
``chat:write`` + ``channels:history`` (and
``groups:history`` for private channels, ``channels:read``/``groups:read`` to
resolve a channel by name), with the bot invited to the channel.

Decides new-vs-thread by CHECKING THE CHANNEL FIRST: it scans recent channel
history for the PR URL and threads the update under that message if present;
only if the PR is not already linked does it post a new message. The message
timestamp saved in RunState is a fallback for when the link has scrolled out of
the scanned history. Best-effort throughout: every call swallows errors and
returns None / does nothing — Slack must never block or fail a review.
"""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request

from crux.models import Config, RepoInfo

log = logging.getLogger("crux.slack")

_BASE = "https://slack.com/api/"
_TIMEOUT = 15
_HISTORY_SCAN = 200  # recent messages scanned for an existing PR link
_CHANNEL_ID = re.compile(r"^[CGD][A-Z0-9]{6,}$")

# Slack "error" codes that mean the bot token itself is bad — a mistyped,
# expired, or revoked token, or a deactivated bot. Unlike a missing scope
# (fixable by adding a permission), these need a fresh token, so they get an
# actionable callout instead of a bare error code buried in the log.
_AUTH_ERRORS = frozenset({
    "invalid_auth", "not_authed", "token_revoked", "token_expired",
    "account_inactive", "no_permission",
})


def _token(cfg: Config) -> str:
    """Resolve the Slack bot token, in order:

    1. the env var named by ``cfg.slack_token_env`` (default
       ``SLACK_BOT_TOKEN``) — the manual override, honored everywhere;
    2. the credentials file (``crux.credentials``,
       ``~/.config/crux/credentials.json``), which Crux reads directly, so it
       is shell-independent: it works under bash/zsh/fish/PowerShell alike and
       when a push comes from an IDE/GUI/agent with no shell env loaded.

    Never raises — an absent/unreadable credentials file (or the import itself
    failing) is treated as "no token", so Slack stays best-effort.
    """
    env = os.environ.get(cfg.slack_token_env or "SLACK_BOT_TOKEN", "").strip()
    if env:
        return env
    try:
        import crux.credentials as credentials
        return credentials.get_slack_bot_token()
    except Exception:
        return ""


def enabled(cfg: Config) -> bool:
    """True only when a channel is configured AND a bot token is present."""
    return bool(cfg.slack_channel and _token(cfg))


def _call(method: str, token: str, *, params: dict | None = None,
          payload: dict | None = None) -> dict | None:
    url = _BASE + method
    data = None
    headers = {"Authorization": f"Bearer {token}"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    elif params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        log.warning("slack %s failed: %s", method, exc)
        return None
    if not body.get("ok"):
        error = body.get("error")
        if error in _AUTH_ERRORS:
            # The token is bad, not just under-scoped — a fresh one is needed.
            log.warning(
                "slack %s error: %s — the bot token is invalid, expired, or "
                "revoked; reinstall the Slack app and refresh your bot token",
                method, error)
            return None
        # missing_scope responses carry a "needed" field naming the scope —
        # without it the log line is undebuggable ("which scope?").
        needed = body.get("needed")
        detail = f" (token needs the {needed} scope)" if needed else ""
        log.warning("slack %s error: %s%s", method, error, detail)
        return None
    return body


def _resolve_channel(cfg: Config, token: str) -> str | None:
    """The channel ID for cfg.slack_channel (an ID passes through; a name is
    looked up via conversations.list).

    Public and private channels are listed SEPARATELY. A combined
    types=public_channel,private_channel request fails whole with
    missing_scope unless the token also has groups:read — so a token with
    exactly the three documented scopes could never resolve any name
    (field-tested). Public listing (channels:read) is required; private
    listing (groups:read) is optional and its absence only matters if the
    name never turns up publicly.
    """
    if _CHANNEL_ID.match(cfg.slack_channel):
        return cfg.slack_channel
    name = cfg.slack_channel.lstrip("#")
    private_blocked = False
    for types in ("public_channel", "private_channel"):
        cursor = ""
        for _ in range(10):  # bounded pagination
            params = {"types": types, "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            body = _call("conversations.list", token, params=params)
            if not body:
                if types == "private_channel":
                    private_blocked = True
                    break
                log.warning(
                    "slack: could not list channels to resolve %r — the "
                    "token needs the channels:read scope; or set [slack] "
                    "channel to the channel ID (Slack: channel → View "
                    "channel details → About → Channel ID), which needs no "
                    "lookup", cfg.slack_channel)
                return None
            for channel in body.get("channels", []):
                if channel.get("name") == name:
                    return channel.get("id")
            cursor = (body.get("response_metadata") or {}).get("next_cursor", "")
            if not cursor:
                break
    if private_blocked:
        log.warning(
            "slack: channel %r is not a public channel this bot can see, and "
            "private channels cannot be listed (token lacks groups:read). If "
            "it is private: add the groups:read + groups:history scopes and "
            "reinstall the app, or set [slack] channel to the channel ID.",
            cfg.slack_channel)
    else:
        log.warning(
            "slack: channel %r not found — check the exact channel name, or "
            "set [slack] channel to the channel ID", cfg.slack_channel)
    return None


def _find_pr_message_ts(token: str, channel_id: str, pr_url: str) -> str | None:
    """Root ts of a recent channel message that links *pr_url*, or None."""
    body = _call("conversations.history", token,
                 params={"channel": channel_id, "limit": _HISTORY_SCAN})
    if not body:
        return None
    for msg in body.get("messages", []):
        if pr_url in (msg.get("text") or ""):
            # thread the reply under the ROOT message, not a reply within it.
            return msg.get("thread_ts") or msg.get("ts")
    return None


def short_name(name: str, login: str = "") -> str:
    """"Firstname Lastname" -> "Firstname L." — who to look at, not who to page.

    A channel announcement is scanned, not read: a first name plus a last
    initial identifies the author among teammates without the line turning
    into an @-mention or a wall of full names on a multi-author bundle. Falls
    back to the GitHub login when the profile has no real name set, which is
    still better than nothing.
    """
    parts = [p for p in (name or "").replace(",", " ").split() if p]
    if not parts:
        return login or ""
    if len(parts) == 1:
        return parts[0]
    # First and LAST token: middle names and initials in between are noise.
    return f"{parts[0]} {parts[-1][0].upper()}."


def names(people: list[str]) -> str:
    """"Fixture A.", "Fixture A. and Fixture B.", "Fixture A. +2".

    A bundle can span authors; past two the names stop being useful and the
    count does the work.
    """
    seen = [n for n in dict.fromkeys(n for n in people if n)]
    if not seen:
        return ""
    if len(seen) == 1:
        return seen[0]
    if len(seen) == 2:
        return f"{seen[0]} and {seen[1]}"
    return f"{seen[0]} +{len(seen) - 1}"


def credit(people: list[str]) -> str:
    """The same list as a trailing credit: "by Fixture A. and Fixture B."."""
    who = names(people)
    return f"by {who}" if who else ""


def super_id(bundle) -> str:
    """"#17" — the bundle as Slack should name it: GitHub's issue number.

    Crux numbers bundles locally, but the brief they link to is a GitHub issue
    with a number of its own, and that is the one a reader can act on: it
    matches the issue they land in, the `owner/repo#17` they can paste
    anywhere, and the number their teammates see. The local number is a
    machine-local counter that means nothing on another laptop. Falls back to
    it only before the brief is filed, when there is nothing else to show.
    """
    return f"#{bundle.issue or bundle.number}"


def repo_names(members: list, cap: int = 4) -> str:
    """"web, api" — the bundle's repos by bare name.

    Bare names, not `owner/repo`: everything in a channel belongs to the same
    handful of orgs, so the owner is noise repeated on every line. Named repos
    beat a count ("3 repos") because a reader recognises the repos they own.
    Past *cap* the line would wrap, so the rest become "+N".
    """
    seen = list(dict.fromkeys(m.repo for m in members if m.repo))
    if len(seen) > cap:
        return f"{', '.join(seen[:cap])} +{len(seen) - cap}"
    return ", ".join(seen)


def announce_pr(cfg: Config, info: RepoInfo, pr: int, title: str, url: str,
                previous_ts: str = "", author: str = "") -> tuple[str, str]:
    """Announce PR *pr* in the configured channel, or thread an update under an
    existing announcement. Returns (channel_id, root_ts) — empty strings when
    Slack is disabled or the call failed.

    Order (per the requirement): check the channel for the PR link FIRST; only
    post a new message if it is not already linked. previous_ts (the saved
    RunState ts) is a fallback for links that scrolled past the scanned history.
    """
    if not enabled(cfg):
        return "", ""
    token = _token(cfg)
    channel_id = _resolve_channel(cfg, token)
    if not channel_id:
        return "", ""

    # The channel usually carries several repos' PRs: name the repo, linked,
    # before the PR link, so a reader knows where the PR lives at a glance.
    repo_link = (f"<https://github.com/{info.owner}/{info.repo}"
                 f"|{info.owner}/{info.repo}>")

    root_ts = _find_pr_message_ts(token, channel_id, url) or previous_ts
    if root_ts:
        _call("chat.postMessage", token, payload={
            "channel": channel_id,
            "thread_ts": root_ts,
            "text": f"🔄 New commits pushed to <{url}|#{pr} {title}>",
        })
        return channel_id, root_ts

    by = credit([author])
    who = f" {by}" if by else ""
    body = _call("chat.postMessage", token, payload={
        "channel": channel_id,
        "unfurl_links": True,
        "text": (f"🔍 New PR up for review{who} in {repo_link}: "
                 f"<{url}|#{pr} {title}>"),
    })
    if not body:
        return "", ""
    return channel_id, body.get("ts", "")


def announce_super(cfg: Config, bundle, url: str, thesis: str = "",
                   previous_ts: str = "", authors: list[str] | None = None,
                   ) -> tuple[str, str]:
    """D37: announce a super PR, or thread an update under its announcement.

    Same shape as `announce_pr` and the same best-effort contract, but keyed on
    the BUNDLE rather than a repo: a cross-repo bundle has no single repo to
    name, so the message is the linked bundle id, who wrote it, and which repos
    it touches. Threading matters more here than for a single PR — a
    bundle is re-briefed as its members move, and each refresh replying in
    thread keeps one conversation instead of N channel posts.

    Returns (channel_id, root_ts); ("", "") when Slack is disabled or the call
    failed.
    """
    if not enabled(cfg):
        return "", ""
    token = _token(cfg)
    channel_id = _resolve_channel(cfg, token)
    if not channel_id:
        return "", ""

    where = repo_names(bundle.members)
    label = (f"Super PR {super_id(bundle)} — {len(bundle.members)} PRs across "
             f"{where}")

    root_ts = _find_pr_message_ts(token, channel_id, url) or previous_ts
    if root_ts:
        _call("chat.postMessage", token, payload={
            "channel": channel_id,
            "thread_ts": root_ts,
            "text": f"🔄 <{url}|{label}> was updated — Crux refreshed the brief.",
        })
        return channel_id, root_ts

    # The identifier IS the link to the brief, so there is no second "read it
    # here" line: one thing to click, named in plain words. "Super PR" spelled
    # out because a bare "#17" would read as an ordinary PR number.
    by = credit(list(authors or []))
    text = f"🦸 <{url}|*Super PR {super_id(bundle)}*>"
    text += f" {by}" if by else ""
    text += f" — {len(bundle.members)} PRs across {where}"
    if thesis:
        text += f"\n_{thesis}_"
    body = _call("chat.postMessage", token, payload={
        "channel": channel_id,
        "unfurl_links": False,  # the brief is a private issue; a preview leaks
        "text": text,
    })
    if not body:
        return "", ""
    return channel_id, body.get("ts", "")


def announce_super_needs_merger(cfg: Config, bundle, url: str, login: str,
                                mine: list, root_ts: str = "") -> tuple[str, str]:
    """D38: ask the channel for someone OTHER than the author to merge.

    Threaded under the bundle's own message, like every other update to it. The
    ask names who cannot press the button and why, because the alternative —
    "can someone merge this?" — reads as impatience rather than as the rule it
    actually is: nobody lands a change they wrote.
    """
    if not enabled(cfg):
        return "", ""
    token = _token(cfg)
    channel_id = _resolve_channel(cfg, token)
    if not channel_id:
        return "", ""

    wrote = ", ".join(f"{m.owner}/{m.repo}#{m.pr}" for m in mine[:4])
    text = (f"🙋 Super PR {super_id(bundle)} is ready to land and needs "
            f"*someone other than @{login}* to merge it — they opened {wrote}, "
            f"so GitHub will not take their approval on it.\n"
            f"Open the brief and press *Merge this super PR*: {url}")

    payload = {"channel": channel_id, "text": text, "unfurl_links": False}
    thread = _find_pr_message_ts(token, channel_id, url) or root_ts
    if thread:
        payload["thread_ts"] = thread
    body = _call("chat.postMessage", token, payload=payload)
    if not body:
        return "", ""
    return channel_id, thread or body.get("ts", "")


def announce_super_merge(cfg: Config, bundle, url: str,
                         results: list, root_ts: str = "",
                         note: str = "") -> tuple[str, str]:
    """D37: report a merge run in the bundle's Slack thread.

    Threaded under the announcement rather than posted fresh: the merge is the
    end of that bundle's story, and a channel-level post would separate it from
    the brief it concludes.
    """
    if not enabled(cfg):
        return "", ""
    token = _token(cfg)
    channel_id = _resolve_channel(cfg, token)
    if not channel_id:
        return "", ""

    landed = [m for m in results if m.state == "merged"]
    blocked = [m for m in results if m.state != "merged"]
    if blocked:
        text = (f"⚠️ Super PR {super_id(bundle)}: {len(landed)} of "
                f"{len(results)} landed. Still blocked:\n"
                + "\n".join(f"• {m.owner}/{m.repo}#{m.pr} — {m.error or 'blocked'}"
                            for m in blocked))
    else:
        text = (f"✅ Super PR {super_id(bundle)} landed — all {len(landed)} "
                f"PR{'s' if len(landed) != 1 else ''} merged.")

    payload = {"channel": channel_id, "text": text, "unfurl_links": False}
    thread = _find_pr_message_ts(token, channel_id, url) or root_ts
    if thread:
        payload["thread_ts"] = thread
    body = _call("chat.postMessage", token, payload=payload)
    if not body:
        return "", ""
    return channel_id, thread or body.get("ts", "")
