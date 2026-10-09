# Copyright © 2026 Sonomos, Inc.
# SPDX-License-Identifier: Apache-2.0

"""Direct GitHub REST calls for hosts without the `gh` CLI.

A host with no `gh` binary and no gh auth has one remaining way to reach
GitHub: a plain HTTPS call carrying a token. This module is that path:
crux.post._run_gh falls back here when `gh` is not installed, re-issuing the
invocation as the equivalent REST request.

Who this actually serves: GENERIC gh-less hosts — CI runners, plain
containers, anywhere with open egress — where a `GH_TOKEN` works normally.
claude.ai/code cloud sessions are NOT that case, and no token makes them one:
their egress proxy answers GitHub API reads with its own credential and
blocks raw writes to api.github.com, so posting from inside the container
fails whatever token is supplied (verified live; see README "Posting goes
through the session's GitHub tools"). There the card is posted by the session
itself through its GitHub MCP tools, off the back of `crux preview` — which
needs no token anywhere.

Token sources, in order:

- ``$GH_TOKEN`` then ``$GITHUB_TOKEN`` — gh's own precedence order, so an
  environment already configured for gh elsewhere works here unchanged.
- ``"github_token"`` in the crux credentials file
  (``~/.config/crux/credentials.json``), same store as the Slack token.

No token => PostError naming the fix for each kind of host; the analysis
itself (``crux preview``) never needs one.

Output is shaped like `gh api` output — the raw JSON response text, with
paginated GETs returning each page's JSON document concatenated — so
crux.post's parsers (including ``_iter_paginated``) behave identically on
both paths.
"""
from __future__ import annotations

import os
import re
import urllib.error
import urllib.request

from crux.credentials import load_credentials
from crux.models import PostError

API_ROOT = "https://api.github.com"

# GitHub caps per_page at 100; ask for it so paginated scans (issue comments)
# need as few round-trips as possible.
_PER_PAGE = 100

# Two different hosts hit this, and they need opposite advice. On a generic
# gh-less host (CI runner, plain container) a token is the fix. In a
# claude.ai/code session it is NOT: the egress proxy blocks raw writes to
# api.github.com whatever token is set, so sending that user off to create one
# is an errand we already know fails — point them at the route that works.
NO_TOKEN_HINT = (
    "gh not installed and no GitHub token found — install gh, or set "
    'GH_TOKEN/GITHUB_TOKEN, or put "github_token" in '
    "~/.config/crux/credentials.json. In a claude.ai/code session no token "
    "helps (its egress proxy blocks writes to api.github.com): run "
    "`crux preview` and post the card from the session itself with the "
    "GitHub MCP tools — the /crux:run command spells out the steps"
)

_LINK_NEXT_RE = re.compile(r'<([^>]+)>\s*;\s*rel="next"')


def github_token() -> str:
    """The GitHub token to call the REST API with, or '' when none is
    configured. Never raises."""
    for var in ("GH_TOKEN", "GITHUB_TOKEN"):
        token = (os.environ.get(var) or "").strip()
        if token:
            return token
    token = load_credentials().get("github_token")
    return token.strip() if isinstance(token, str) else ""


def _next_link(link_header: str) -> str | None:
    """The rel="next" URL from a Link response header, or None on the last
    page (or no header at all)."""
    match = _LINK_NEXT_RE.search(link_header or "")
    return match.group(1) if match else None


def rest_call(method: str, path: str, body_text: str | None = None,
              paginate: bool = False, timeout: int = 120) -> str:
    """Issue ``METHOD https://api.github.com/<path>`` and return the response
    text, shaped like `gh api` stdout. Any failure raises PostError.

    *body_text* is sent verbatim as the JSON request body (callers already
    hold json.dumps output — the same string they pipe to `gh api --input -`).
    *paginate* follows Link rel="next" and concatenates the pages' documents,
    which is exactly what ``gh api --paginate`` emits.
    """
    token = github_token()
    if not token:
        raise PostError(NO_TOKEN_HINT)
    url = path if path.startswith("https://") else f"{API_ROOT}/{path.lstrip('/')}"
    if paginate and "per_page=" not in url:
        url += ("&" if "?" in url else "?") + f"per_page={_PER_PAGE}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "crux",
    }
    if body_text is not None:
        headers["Content-Type"] = "application/json"
    pages: list[str] = []
    while url:
        request = urllib.request.Request(
            url, method=method.upper(),
            data=body_text.encode("utf-8") if body_text is not None else None,
            headers=headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                pages.append(response.read().decode("utf-8", "replace"))
                link = str(response.headers.get("Link") or "")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = " " + " ".join(
                    exc.read().decode("utf-8", "replace").split())[:300]
            except OSError:
                pass
            raise PostError(
                f"GitHub API {method} {path} failed: HTTP {exc.code}{detail}"
            ) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise PostError(f"GitHub API {method} {path} failed: {exc}") from None
        url = _next_link(link) if paginate else None
    return "\n".join(pages)
