# Copyright © 2026 Sonomos, Inc.
# SPDX-License-Identifier: Apache-2.0

"""D40: Zenhub — the GraphQL layer.

What this is for: a ticket in Zenhub and the pull request that implements it
are two records of one piece of work, and keeping them in step is bookkeeping
nobody enjoys. Crux already knows when a PR lands (it is the thing that lands
it), so it is the natural place to close the ticket too.

**Entirely optional.** ``[zenhub] workspace`` is empty by default, and while it
is empty ``enabled()`` is False and every caller skips — no calls, no prompts,
no behaviour change anywhere. Turning it on is one line of config plus a key.

Auth mirrors Slack (``crux/slack.py``) exactly, for the same reason: the env
var named by ``cfg.zenhub_token_env`` first (the override), else
``zenhub_api_key`` in the credentials file, which Crux reads itself so it works
under any shell and when a push comes from an IDE/GUI/agent with no shell env
loaded. Keys are made at https://app.zenhub.com/settings/tokens.

Best-effort throughout, again like Slack: every call swallows its errors and
returns None. Zenhub must never block or fail a review or a merge — a ticket
left open is a nuisance, a merge that refuses to run is not.

    Schema note. The queries below are grouped in ONE block on purpose: they
    are the only thing that has to change if Zenhub's schema moves under us.
    ``viewer{searchWorkspaces}``, ``workspace(id){issues}``, ``closeIssues`` and
    ``reopenIssues`` come from Zenhub's published examples; ``issueByInfo``,
    ``moveIssue`` and ``createIssuePrConnection`` are documented more thinly
    (the last one barely at all). ``crux zenhub doctor`` introspects the live
    schema and reports which of these the server actually accepts, so a drift
    shows up as a diagnosis instead of a mystery.
"""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request

from crux.models import Config, CruxError, ZenTicket

log = logging.getLogger("crux.zenhub")

ENDPOINT = "https://api.zenhub.com/public/graphql"
_TIMEOUT = 20
# Pages of 100 issues pulled when listing a workspace's open tickets. 10 pages
# is 1000 issues — past that a picker is not a picker, and the caller should
# be narrowing with a query instead of scrolling.
_MAX_PAGES = 10
# Zenhub workspace IDs are Mongo ObjectIds. Anything else configured is a name
# to look up, which saves the user hunting an ID out of a URL.
_WORKSPACE_ID = re.compile(r"^[0-9a-f]{24}$")

# Resolved once per process: a workspace name costs a round trip to turn into
# an ID, and a single `crux run` may link several tickets.
_ws_cache: dict[str, str] = {}
_ghid_cache: dict[str, int] = {}


# ---------------------------------------------------------------------------
# The GraphQL documents — the whole schema surface Crux depends on
# ---------------------------------------------------------------------------

# searchWorkspaces hangs off `viewer`, not the root Query: asked at the root,
# the server rejects the whole document and every name resolves to nothing.
Q_WORKSPACE_BY_NAME = """
query CruxFindWorkspace($q: String!) {
  viewer { searchWorkspaces(query: $q) { nodes { id name } } }
}
"""

Q_PIPELINES = """
query CruxPipelines($id: ID!) {
  workspace(id: $id) { id name pipelinesConnection { nodes { id name } } }
}
"""

# Zenhub sorts issue types into board work (Task, Bug, Feature) and planning
# items (Epic, Project, Initiative) by `disposition`. Crux only links and closes
# board work: a PR finishes a task, never the epic or project above it.
_ISSUE_TYPE = """
fragment CruxIssueType on IssueIssueType {
  ... on ZenhubIssueType { name disposition }
  ... on GithubIssueType { name disposition }
}
"""
_PLANNING = "PLANNING_PANEL"

Q_OPEN_ISSUES = _ISSUE_TYPE + """
query CruxOpenIssues($id: ID!, $after: String) {
  workspace(id: $id) {
    issues(after: $after, first: 100) {
      pageInfo { hasNextPage endCursor }
      nodes {
        id number title body state pullRequest htmlUrl
        issueType { ...CruxIssueType }
        repository { ghId name ownerName }
        pipelineIssue(workspaceId: $id) { pipeline { name } }
      }
    }
  }
}
"""

Q_ISSUE_BY_INFO = _ISSUE_TYPE + """
query CruxIssueByInfo($repo: Int!, $number: Int!) {
  issueByInfo(repositoryGhId: $repo, issueNumber: $number) {
    id number title body state pullRequest htmlUrl
    issueType { ...CruxIssueType }
    repository { ghId name ownerName }
  }
}
"""

M_CLOSE_ISSUES = """
mutation CruxCloseIssues($input: CloseIssuesInput!) {
  closeIssues(input: $input) { clientMutationId }
}
"""

M_MOVE_ISSUE = """
mutation CruxMoveIssue($input: MoveIssueInput!) {
  moveIssue(input: $input) { issue { id } }
}
"""

M_CONNECT_PR = """
mutation CruxConnectPr($input: CreateIssuePrConnectionInput!) {
  createIssuePrConnection(input: $input) { issue { id } }
}
"""

Q_INTROSPECT = """
query CruxIntrospect {
  __schema {
    queryType { fields { name } }
    mutationType { fields { name } }
  }
}
"""


# ---------------------------------------------------------------------------
# Auth and the enable switch
# ---------------------------------------------------------------------------

def _token(cfg: Config) -> str:
    """The Zenhub personal API key: env var first, then the credentials file.

    Never raises — an absent or unreadable credentials file (or the import
    itself failing) reads as "no key", which reads as "Zenhub is off".
    """
    env = os.environ.get(cfg.zenhub_token_env or "ZENHUB_API_KEY", "").strip()
    if env:
        return env
    try:
        import crux.credentials as credentials
        return credentials.get_zenhub_api_key()
    except Exception:
        return ""


def enabled(cfg: Config) -> bool:
    """True only when a workspace is configured AND a key is present.

    Both halves matter: a workspace with no key cannot be reached, and a key
    with no workspace has nothing to point at. Either missing means Crux does
    nothing Zenhub-shaped anywhere, which is the default state.
    """
    return bool(cfg.zenhub_workspace and _token(cfg))


def why_disabled(cfg: Config) -> str:
    """A sentence naming what is missing, for commands a human asked for.

    The automatic paths stay silent when Zenhub is off — that is the point of
    off. But someone who typed `crux zenhub link` is owed a reason.
    """
    if not cfg.zenhub_workspace and not _token(cfg):
        return ('Zenhub is not set up: set [zenhub] workspace in crux.toml and '
                'store a key with `crux zenhub setup`.')
    if not cfg.zenhub_workspace:
        return ('Zenhub has a key but no workspace: set [zenhub] workspace in '
                'crux.toml (a workspace name, or its ID from the app URL).')
    return (f'Zenhub has a workspace but no API key: set '
            f'${cfg.zenhub_token_env} or run `crux zenhub setup` (keys are '
            f'made at https://app.zenhub.com/settings/tokens).')


# ---------------------------------------------------------------------------
# The transport
# ---------------------------------------------------------------------------

def _call(cfg: Config, query: str, variables: dict | None = None,
          op: str = "") -> dict | None:
    """POST one GraphQL document. Returns the `data` object, or None.

    GraphQL answers 200 with an `errors` array, so a failure never arrives as
    an exception — it has to be read out of the body. A field error (the
    schema moved) is called out by name rather than logged as a generic
    failure, because that is the one failure `crux zenhub doctor` exists for.
    """
    token = _token(cfg)
    if not token:
        return None
    payload = json.dumps({"query": query, "variables": variables or {}})
    req = urllib.request.Request(
        ENDPOINT, data=payload.encode("utf-8"),
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:200]
        except Exception:
            pass
        if exc.code in (401, 403):
            log.warning("zenhub %s: the API key was rejected (HTTP %d) — make "
                        "a fresh one at https://app.zenhub.com/settings/tokens",
                        op or "call", exc.code)
        else:
            log.warning("zenhub %s failed: HTTP %d %s", op or "call", exc.code, detail)
        return None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        log.warning("zenhub %s failed: %s", op or "call", exc)
        return None

    errors = body.get("errors")
    if errors:
        messages = [str(e.get("message", e)) for e in errors if isinstance(e, dict)]
        joined = "; ".join(messages) or str(errors)
        if any("Cannot query field" in m or "Unknown argument" in m
               or "Unknown type" in m for m in messages):
            log.warning("zenhub %s: this Crux does not match the Zenhub schema "
                        "(%s) — run `crux zenhub doctor`", op or "call", joined)
        else:
            log.warning("zenhub %s error: %s", op or "call", joined)
        return None
    data = body.get("data")
    return data if isinstance(data, dict) else None


# ---------------------------------------------------------------------------
# Workspace and pipelines
# ---------------------------------------------------------------------------

def workspace_id(cfg: Config) -> str:
    """The configured workspace as an ID, looking a name up when needed.

    A name is what people know their workspace by; the ID is buried in the
    app URL. Accepting either means the config line can be typed from memory.
    """
    configured = (cfg.zenhub_workspace or "").strip()
    if not configured:
        return ""
    if _WORKSPACE_ID.match(configured):
        return configured
    if configured in _ws_cache:
        return _ws_cache[configured]
    data = _call(cfg, Q_WORKSPACE_BY_NAME, {"q": configured}, op="searchWorkspaces")
    viewer = (data or {}).get("viewer") or {}
    nodes = ((viewer.get("searchWorkspaces") or {}).get("nodes")) or []
    exact = [n for n in nodes if isinstance(n, dict)
             and (n.get("name") or "").strip().lower() == configured.lower()]
    chosen = (exact or [n for n in nodes if isinstance(n, dict)])[:1]
    if not chosen:
        log.warning("zenhub: no workspace named %r — check [zenhub] workspace, "
                    "or set it to the workspace ID from the app URL", configured)
        return ""
    found = str(chosen[0].get("id") or "")
    if found:
        _ws_cache[configured] = found
    return found


def pipelines(cfg: Config) -> list[tuple[str, str]]:
    """[(pipeline_id, name)] for the configured workspace, in board order."""
    wid = workspace_id(cfg)
    if not wid:
        return []
    data = _call(cfg, Q_PIPELINES, {"id": wid}, op="pipelines")
    nodes = ((((data or {}).get("workspace") or {})
              .get("pipelinesConnection") or {}).get("nodes")) or []
    return [(str(n.get("id") or ""), str(n.get("name") or ""))
            for n in nodes if isinstance(n, dict) and n.get("id")]


def done_pipeline_id(cfg: Config) -> str:
    """The ID of the pipeline named by ``[zenhub] done_pipeline``, or ''.

    An unset name means "close without moving", which is a legitimate choice:
    plenty of boards let the closed state do the work. A name that matches no
    pipeline is a typo and says so, because silently not moving the card is
    exactly the bookkeeping gap this feature exists to close.
    """
    wanted = (cfg.zenhub_done_pipeline or "").strip()
    if not wanted:
        return ""
    found = [pid for pid, name in pipelines(cfg)
             if name.strip().lower() == wanted.lower()]
    if not found:
        known = ", ".join(name for _, name in pipelines(cfg)) or "none readable"
        log.warning("zenhub: no pipeline named %r in this workspace (have: %s) "
                    "— tickets will be closed but not moved", wanted, known)
        return ""
    return found[0]


# ---------------------------------------------------------------------------
# Issues
# ---------------------------------------------------------------------------

def _ticket(node: dict, pipeline: str = "") -> ZenTicket:
    repo = node.get("repository") or {}
    pipe = pipeline
    if not pipe:
        holder = node.get("pipelineIssue") or {}
        pipe = str(((holder.get("pipeline") or {}).get("name")) or "")
    kind = node.get("issueType") or {}
    return ZenTicket(
        id=str(node.get("id") or ""),
        number=int(node.get("number") or 0),
        owner=str(repo.get("ownerName") or ""),
        repo=str(repo.get("name") or ""),
        title=str(node.get("title") or ""),
        body=str(node.get("body") or ""),
        url=str(node.get("htmlUrl") or ""),
        pipeline=pipe,
        kind=str(kind.get("name") or ""),
        planning=kind.get("disposition") == _PLANNING,
    )


def open_issues(cfg: Config) -> list[ZenTicket]:
    """Every open, non-PR, non-planning issue in the workspace, oldest first.

    Zenhub pages these 100 at a time and offers no server-side open/closed
    filter on the workspace connection, so the filtering happens here. Bounded
    at _MAX_PAGES: a picker that has to page a thousand tickets is the wrong
    tool, and the caller should be passing a query.
    """
    wid = workspace_id(cfg)
    if not wid:
        return []
    out: list[ZenTicket] = []
    after: str | None = None
    for _ in range(_MAX_PAGES):
        data = _call(cfg, Q_OPEN_ISSUES, {"id": wid, "after": after},
                     op="workspace.issues")
        issues = (((data or {}).get("workspace") or {}).get("issues")) or {}
        for node in issues.get("nodes") or []:
            if not isinstance(node, dict):
                continue
            if node.get("pullRequest"):
                continue  # a PR is not a ticket to close
            if str(node.get("state") or "").upper() == "CLOSED":
                continue
            ticket = _ticket(node)
            if ticket.planning:
                continue  # an epic/project is closed by people, not by a PR
            out.append(ticket)
        page = issues.get("pageInfo") or {}
        if not page.get("hasNextPage"):
            break
        after = page.get("endCursor")
        if not after:
            break
    return out


def repo_ghid(slug: str) -> int:
    """GitHub's numeric ID for `owner/name` — what Zenhub keys repos by.

    Zenhub identifies a repository by its GitHub database ID, not its slug, so
    every issue lookup starts here. Cached: a bundle asks about the same
    handful of repos repeatedly.
    """
    if slug in _ghid_cache:
        return _ghid_cache[slug]
    import crux.post as post
    try:
        out = post._run_gh(["api", f"repos/{slug}", "--jq", ".id"])
        value = int(str(out).strip())
    except (CruxError, TypeError, ValueError) as exc:
        log.warning("zenhub: could not read the GitHub ID of %s (%s)", slug, exc)
        return 0
    _ghid_cache[slug] = value
    return value


def issue_by_ref(cfg: Config, slug: str, number: int) -> ZenTicket | None:
    """Resolve `owner/repo#N` to a Zenhub ticket, for an explicit --issue."""
    ghid = repo_ghid(slug)
    if not ghid:
        return None
    data = _call(cfg, Q_ISSUE_BY_INFO, {"repo": ghid, "number": int(number)},
                 op="issueByInfo")
    node = (data or {}).get("issueByInfo")
    if not isinstance(node, dict) or not node.get("id"):
        log.warning("zenhub: no issue %s#%d in this workspace", slug, number)
        return None
    if node.get("pullRequest"):
        log.warning("zenhub: %s#%d is a pull request, not a ticket", slug, number)
        return None
    return _ticket(node)


def pr_node_id(cfg: Config, slug: str, pr: int) -> str:
    """The Zenhub node ID of a PULL REQUEST, for the connection mutation.

    Zenhub models pull requests through the same lookup as issues, so this is
    ``issueByInfo`` again with the PR's number — the one place we WANT the
    ``pullRequest`` flag set. Returns '' when it cannot be resolved, which the
    caller treats as "no visual connection", never as a failure.
    """
    ghid = repo_ghid(slug)
    if not ghid:
        return ""
    data = _call(cfg, Q_ISSUE_BY_INFO, {"repo": ghid, "number": int(pr)},
                 op="issueByInfo(pr)")
    node = (data or {}).get("issueByInfo")
    if not isinstance(node, dict):
        return ""
    return str(node.get("id") or "")


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------

def connect_pr(cfg: Config, ticket: ZenTicket, slug: str, pr: int) -> bool:
    """Draw Zenhub's own issue↔PR connection. Returns whether it stuck.

    This is the thinnest-documented call in the whole module, so it is also
    the most expendable: the link Crux relies on lives in its own store and in
    the PR body, and both survive this failing. What is lost is the connection
    drawn on the Zenhub card — cosmetic, and worth attempting for exactly that
    reason, not worth failing a run over.
    """
    if not ticket.id:
        return False
    pr_id = pr_node_id(cfg, slug, pr)
    if not pr_id:
        return False
    data = _call(cfg, M_CONNECT_PR,
                 {"input": {"issueId": ticket.id, "pullRequestId": pr_id}},
                 op="createIssuePrConnection")
    return bool(data)


def close_tickets(cfg: Config, tickets: list[ZenTicket]) -> list[str]:
    """Close *tickets* and move them to the done pipeline. Returns problems.

    Two steps, because they are two different statements. `closeIssues` closes
    the ticket — and with it the GitHub issue behind it, which is what makes
    the GitHub side of the bookkeeping right. The pipeline move is what makes
    the BOARD right, and boards do not always move a card on close by
    themselves. Doing both is the whole point of the feature.

    One batch for the close (the mutation takes a list) and one call per
    ticket for the move (it does not).
    """
    for t in tickets:
        if t.planning:
            log.warning("zenhub: not closing %s — %s is a planning item, not a task",
                        describe(t), t.kind or "it")
    live = [t for t in tickets if t.id and not t.planning]
    if not live:
        return []
    problems: list[str] = []
    data = _call(cfg, M_CLOSE_ISSUES, {"input": {"issueIds": [t.id for t in live]}},
                 op="closeIssues")
    if data is None:
        return [f"could not close {describe(t)} in Zenhub" for t in live]
    log.info("closed %d Zenhub ticket(s): %s",
             len(live), ", ".join(describe(t) for t in live))

    pipeline = done_pipeline_id(cfg)
    if not pipeline:
        return problems
    wid = workspace_id(cfg)
    for ticket in live:
        moved = _call(cfg, M_MOVE_ISSUE, {"input": {
            "issueId": ticket.id, "pipelineId": pipeline,
            "workspaceId": wid, "position": "TOP"}}, op="moveIssue")
        if moved is None:
            problems.append(
                f"{describe(ticket)} was closed but could not be moved to "
                f"{cfg.zenhub_done_pipeline!r}")
    return problems


# ---------------------------------------------------------------------------
# Diagnosis
# ---------------------------------------------------------------------------

def introspect(cfg: Config) -> tuple[list[str], list[str]]:
    """(query fields, mutation fields) the live server exposes.

    The answer to "why did nothing happen?" when Zenhub's schema has moved.
    """
    data = _call(cfg, Q_INTROSPECT, op="introspection")
    schema = (data or {}).get("__schema") or {}
    def names(holder):
        return sorted(str(f.get("name")) for f in (holder or {}).get("fields") or []
                      if isinstance(f, dict) and f.get("name"))
    return names(schema.get("queryType")), names(schema.get("mutationType"))


def describe(ticket: ZenTicket) -> str:
    """"owner/repo#29" — how a human refers to a ticket."""
    if ticket.owner and ticket.repo:
        return f"{ticket.owner}/{ticket.repo}#{ticket.number}"
    return f"#{ticket.number}"
