# Copyright © 2026 Sonomos, Inc.
# SPDX-License-Identifier: Apache-2.0

"""D38: the loopback service behind the brief's buttons.

`crux serve` listens on 127.0.0.1 and nowhere else. The brief's action links
point at that address, and the SAME link works for everyone who reads the
brief, because loopback resolves on the machine of whoever clicked it. That is
the entire mechanism, and it is what makes an approval possible at all: the
request lands on a Crux already logged in as that person, so `gh` can approve
in their name. A hosted service would have to hold everyone's GitHub token, and
a CI token approves as a bot.

Two rules shape the request handling:

**GET renders, POST acts.** Links inside issue bodies get fetched by things
that are not people — Slack unfurls, browser prefetch, corporate URL scanners.
A GET that merged would eventually fire itself with nobody present. So GET
returns a page describing exactly what will happen, and only a form POST from
that page does anything. The form carries a token minted by this process, and
the handler additionally refuses cross-site POSTs, so a random page in another
tab cannot post to the port either.

**Bound to loopback, and that is the security boundary.** Anything already
running as this user could run `gh` itself, so the port grants nothing new
locally. It grants nothing remotely because it is not reachable remotely.
"""
from __future__ import annotations

import html
import http.server
import json
import logging
import os
import secrets
import signal
import threading
import time
import urllib.parse

import crux.bundle as bundle_store
import crux.superact as superact
import crux.superpost as superpost
from crux.models import Bundle, Config, CruxError

log = logging.getLogger("crux.serve")

# One per process. It has to survive only from the confirm page to its own
# POST, so a restart invalidating open pages is correct, not a bug.
_TOKEN = secrets.token_urlsafe(24)

_CSS = """
:root { color-scheme: light dark; }
body { font: 16px/1.6 system-ui, sans-serif; max-width: 44rem;
       margin: 3rem auto; padding: 0 1.5rem; }
h1 { font-size: 1.4rem; margin-bottom: .2rem; }
.sub { opacity: .7; margin-top: 0; }
ul { padding-left: 1.2rem; }
li { margin: .3rem 0; }
form { display: inline; }
button { font: inherit; padding: .6rem 1.1rem; border-radius: .5rem;
         border: 1px solid currentColor; background: transparent;
         cursor: pointer; margin: .3rem .4rem .3rem 0; }
button.go { background: #1f883d; border-color: #1f883d; color: #fff; }
button.danger { border-color: #cf222e; color: #cf222e; }
.stop { border-left: 3px solid #cf222e; padding-left: 1rem; }
.ok { border-left: 3px solid #1f883d; padding-left: 1rem; }
code { background: rgba(127,127,127,.18); padding: .1rem .3rem;
       border-radius: .25rem; }
"""


# The last thing between a mis-click and a merge. The page's own POST guard
# stops other origins; this stops the hand that meant to press something else,
# which on a page whose buttons are all irreversible is the likelier accident.
# The wording lives in a data attribute rather than an inline handler so the
# text is HTML-escaped once and never has to survive being parsed as JavaScript
# — an apostrophe in a repo name would otherwise break the dialog open.
_CONFIRM_JS = """
for (const f of document.querySelectorAll('form[data-confirm]')) {
  f.addEventListener('submit', e => {
    if (!window.confirm(f.dataset.confirm)) e.preventDefault();
  });
}
"""


def _page(title: str, body: str) -> bytes:
    return (f"<!doctype html><meta charset=utf-8>"
            f"<meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{html.escape(title)}</title><style>{_CSS}</style>"
            f"{body}<script>{_CONFIRM_JS}</script>").encode("utf-8")


def _form(action: str, label: str, css: str = "go", confirm: str = "",
          **fields: str) -> str:
    hidden = "".join(
        f"<input type=hidden name={k} value='{html.escape(v)}'>"
        for k, v in {"token": _TOKEN, **fields}.items())
    ask = f' data-confirm="{html.escape(confirm, quote=True)}"' if confirm else ""
    return (f"<form method=post action='{html.escape(action)}'{ask}>{hidden}"
            f"<button class='{css}'>{html.escape(label)}</button></form>")


def _members_list(bundle: Bundle) -> str:
    from crux.supermerge import order_members
    rows = []
    for member in order_members(bundle):
        slug = f"{member.owner}/{member.repo}"
        state = " — already merged" if member.state == "merged" else ""
        rows.append(f"<li><code>{html.escape(slug)}#{member.pr}</code>"
                    f"{html.escape(state)}</li>")
    return "<ul>" + "".join(rows) + "</ul>"


def _method_note(bundle: Bundle, cfg: Config) -> str:
    """D41: how the merge below will land. Said on the page because it is
    irreversible and may not be what the reader assumes — a bundle built on
    each other's commits needs merge commits, and a squash would strand the
    later PRs in conflicts."""
    from crux.supermerge import method_label, resolve_method
    label = method_label(resolve_method(bundle, cfg))
    where = ("set on this super PR" if bundle.merge_method
             else "the default on this machine")
    return (f"<p class=sub>Merge method: <b>{html.escape(label)}</b> — "
            f"{html.escape(where)}</p>")


def _brief_link(bundle: Bundle) -> str:
    if not bundle.issue:
        return ""
    url = superpost.issue_url(bundle.home, bundle.issue)
    return f"<p><a href='{html.escape(url)}'>Open the brief on GitHub</a></p>"


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

def merge_page(bundle: Bundle, cfg: Config) -> bytes:
    """The confirm page — or the refusal, when the reader wrote the work."""
    head = (f"<h1>🦸 Super PR #{bundle.number}"
            f"{html.escape(': ' + bundle.name if bundle.name else '')}</h1>")
    try:
        login = superact.actor_login()
        mine = superact.authored_by(bundle, login)
    except CruxError as exc:
        return _page("Crux", head + f"<p class=stop>{html.escape(str(exc))}</p>")

    count = len(bundle.members)
    admin = _form(
        f"/super/{bundle.number}/admin-merge",
        "Admin merge — no approvals, recorded", "danger",
        confirm=(f"Merge all {count} pull requests in Super PR "
                 f"#{bundle.number} on admin rights, with NO approval recorded "
                 f"on any of them?\n\nWho overrode is written on the brief and "
                 f"in Slack. This cannot be undone."))

    if len(mine) == len(bundle.members):
        # The gate lives here, not in the card: GitHub renders one issue body
        # for everyone, so the button cannot be hidden from its author. It is
        # shown to all and works for someone else — or, on the record, for an
        # admin who has already read the work.
        return _page(f"Super PR #{bundle.number}", head + (
            f"<p class=sub>Signed in as {html.escape(login)}</p>"
            f"<p class=stop>{html.escape(superact.refusal(bundle, login, mine))}</p>"
            f"{_members_list(bundle)}"
            f"{_method_note(bundle, cfg)}"
            f"{_form(f'/super/{bundle.number}/ask', 'Ask in Slack for a merge')}"
            f"{admin}"
            f"{_brief_link(bundle)}"))

    yours = ""
    if mine:
        # Wrote part of it: the rest is other people's work and you are a real
        # reviewer of it, so it goes ahead — and yours is named up front rather
        # than turning up as a surprise failure in the report.
        named = ", ".join(f"{m.owner}/{m.repo}#{m.pr}" for m in mine)
        yours = (f"<p class=stop>You opened {html.escape(named)}. That one is "
                 f"skipped and reported — someone else has to approve it — and "
                 f"the rest go ahead.</p>")

    return _page(f"Super PR #{bundle.number}", head + (
        f"<p class=sub>Signed in as {html.escape(login)} — approving as you</p>"
        f"{yours}"
        f"<p>This approves each pull request below in your name, then merges "
        f"them in this order:</p>{_members_list(bundle)}"
        f"{_method_note(bundle, cfg)}"
        f"{_form(f'/super/{bundle.number}/merge', 'Approve and merge all', confirm=(f'Approve all {count} pull requests in Super PR #{bundle.number} in your name and merge them?' + chr(10) + chr(10) + 'This cannot be undone.'))}"
        f"{admin}"
        f"{_brief_link(bundle)}"))


def close_page(bundle: Bundle) -> bytes:
    open_prs = [m for m in bundle.members if m.state != "merged"]
    head = (f"<h1>🦸 Super PR #{bundle.number}"
            f"{html.escape(': ' + bundle.name if bundle.name else '')}</h1>")
    # Two buttons, never one with a checkbox: closing other people's open work
    # in several repos is a different decision from retiring a reading of it,
    # and it should cost a separate, clearly-labelled press.
    return _page(f"Close super PR #{bundle.number}", head + (
        f"<p>Closing the brief retires this bundle. The pull requests stay "
        f"open unless you say otherwise.</p>{_members_list(bundle)}"
        f"{_form(f'/super/{bundle.number}/close', 'Close the brief', confirm=f'Close the brief for Super PR #{bundle.number}? The pull requests stay open.')}"
        f"{_form(f'/super/{bundle.number}/close', f'Close the brief and all {len(open_prs)} PRs', 'danger', prs='1', confirm=(f'Close the brief for Super PR #{bundle.number} AND all {len(open_prs)} of its pull requests?' + chr(10) + chr(10) + 'Open work in several repos will be closed, possibly other people' + chr(39) + 's.'))}"
        f"{_brief_link(bundle)}"))


def checkout_page(bundle: Bundle) -> bytes:
    """What "set up to test" is about to do, before it touches any clone."""
    head = (f"<h1>🦸 Super PR #{bundle.number}"
            f"{html.escape(': ' + bundle.name if bundle.name else '')}</h1>")
    rows = "".join(
        f"<li><code>{html.escape(m.owner)}/{html.escape(m.repo)}</code> → "
        f"<code>{html.escape(m.branch)}</code></li>" for m in bundle.members)
    return _page(f"Set up super PR #{bundle.number}", head + (
        f"<p>This puts every repo below on its pull request branch and brings "
        f"it up to date with the remote, so the whole feature can be tried at "
        f"once:</p><ul>{rows}</ul>"
        f"<p class=sub>Any you do not have yet are cloned first. Nothing is "
        f"merged, rebased or stashed — a repo with uncommitted changes is "
        f"reported and left alone.</p>"
        f"{_form(f'/super/{bundle.number}/checkout', 'Check them all out')}"
        f"{_brief_link(bundle)}"))


def do_checkout(bundle: Bundle, cfg: Config) -> bytes:
    results = superact.checkout(bundle, cfg)
    rows = "".join(
        f"<li>{r.mark} <code>{html.escape(r.slug)}</code> — "
        f"{html.escape(r.detail)}"
        + (f"<br><span class=sub><code>{html.escape(r.path)}</code></span>"
           if r.path else "")
        + "</li>"
        for r in results)
    ready = sum(1 for r in results if r.state == "ready")
    body = (f"<h1>{ready} of {len(results)} repos ready</h1><ul>{rows}</ul>")
    # The steps are why someone pressed this. Showing them here saves the trip
    # back to the issue to find what they were setting up for.
    if bundle.test_steps:
        steps = "".join(f"<li>{html.escape(s)}</li>" for s in bundle.test_steps)
        body += f"<h2>🧪 How to verify this</h2><ol>{steps}</ol>"
    return _page("Ready to test", body + _brief_link(bundle))


def _result(title: str, lines: list[str], bundle: Bundle) -> bytes:
    items = "".join(f"<li>{html.escape(line)}</li>" for line in lines)
    return _page(title, f"<h1>{html.escape(title)}</h1><ul>{items}</ul>"
                        f"{_brief_link(bundle)}")


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

def do_merge(bundle: Bundle, cfg: Config) -> bytes:
    try:
        results, line, problems = superact.merge(bundle, cfg)
    except CruxError as exc:
        return _page("Not merged",
                     f"<h1>Not merged</h1><p class=stop>{html.escape(str(exc))}</p>"
                     + _form(f"/super/{bundle.number}/ask",
                             "Ask in Slack for a merge")
                     + _brief_link(bundle))
    rows = [f"{m.owner}/{m.repo}#{m.pr} — "
            + ("merged" if m.state == "merged" else (m.error or "blocked"))
            for m in results]
    return _result(line, rows + problems, bundle)


def do_admin_merge(bundle: Bundle, cfg: Config) -> bytes:
    try:
        results, line, _ = superact.admin_merge(bundle, cfg)
    except CruxError as exc:
        return _page("Not merged", f"<h1>Not merged</h1>"
                                   f"<p class=stop>{html.escape(str(exc))}</p>"
                                   + _brief_link(bundle))
    rows = [f"{m.owner}/{m.repo}#{m.pr} — "
            + ("merged with no approval" if m.state == "merged"
               else (m.error or "blocked"))
            for m in results]
    return _result(line, rows, bundle)


# ---------------------------------------------------------------------------
# One ordinary PR — the same buttons on the per-PR card
# ---------------------------------------------------------------------------

def pr_page(slug: str, pr: int) -> bytes:
    head = f"<h1>{html.escape(slug)}#{pr}</h1>"
    admin = _form(
        f"/pr/{slug}/{pr}/admin-merge",
        "Admin merge — no approval, recorded", "danger",
        confirm=(f"Merge {slug}#{pr} on admin rights, with NO approval "
                 f"recorded?\n\nWho overrode is written on the pull request. "
                 f"This cannot be undone."))
    try:
        login = superact.actor_login()
        author = superact.pr_author(slug, pr)
    except CruxError as exc:
        return _page("Crux", head + f"<p class=stop>{html.escape(str(exc))}</p>")
    if author.lower() == login.lower():
        return _page(f"{slug}#{pr}", head + (
            f"<p class=sub>Signed in as {html.escape(login)}</p>"
            f"<p class=stop>You opened this pull request, so it is not yours to "
            f"merge — GitHub will not take your approval on your own work. "
            f"Someone else has to press this.</p>{admin}"))
    return _page(f"{slug}#{pr}", head + (
        f"<p class=sub>Signed in as {html.escape(login)} — approving as you</p>"
        f"<p>This approves {html.escape(slug)}#{pr} in your name and merges "
        f"it.</p>"
        f"{_form(f'/pr/{slug}/{pr}/merge', 'Approve and merge', confirm=(f'Approve {slug}#{pr} in your name and merge it?' + chr(10) + chr(10) + 'This cannot be undone.'))}{admin}"))


def do_pr_merge(slug: str, pr: int, admin: bool = False) -> bytes:
    action = superact.admin_merge_pr if admin else superact.merge_pr
    try:
        merged, detail = action(slug, pr)
    except CruxError as exc:
        return _page("Not merged", f"<h1>Not merged</h1>"
                                   f"<p class=stop>{html.escape(str(exc))}</p>")
    title = "Merged" if merged else "Not merged"
    css = "ok" if merged else "stop"
    return _page(title, f"<h1>{title}</h1>"
                        f"<p class={css}>{html.escape(detail)}</p>"
                        f"<p><a href='https://github.com/{html.escape(slug)}"
                        f"/pull/{pr}'>Open the pull request</a></p>")


def do_close(bundle: Bundle, cfg: Config, with_prs: bool) -> bytes:
    problems = superact.close(bundle, cfg, with_prs=with_prs)
    done = [f"brief closed — super PR #{bundle.number} retired"]
    if with_prs:
        done += [f"{m.owner}/{m.repo}#{m.pr} — closed"
                 for m in bundle.members if m.state == "closed"]
    return _result("Closed", done + problems, bundle)


def do_ask(bundle: Bundle, cfg: Config) -> bytes:
    try:
        login = superact.actor_login()
        mine = superact.authored_by(bundle, login)
    except CruxError as exc:
        return _page("Crux", f"<p class=stop>{html.escape(str(exc))}</p>")
    said = superact.ask_for_merge(bundle, cfg, login, mine)
    return _page("Asked", f"<h1>Asked</h1><p class=ok>{html.escape(said)}</p>"
                          + _brief_link(bundle))


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "crux"
    cfg = Config()

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        log.info("%s - %s", self.address_string(), fmt % args)

    # -- helpers ---------------------------------------------------------
    def _send(self, body: bytes, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # Nothing here should ever be cached, least of all by something that
        # replays it later.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _bundle(self, number: int) -> Bundle | None:
        """This machine's bundle, or the one rebuilt from its brief (D38).

        A reader of the brief is usually NOT the machine that made the bundle —
        that is the whole point of the buttons — so "not found here" must never
        be the answer a teammate gets.
        """
        try:
            return bundle_store.hydrate(number, self.cfg)
        except CruxError:
            return None

    def _route(self) -> tuple[int, str] | None:
        parts = urllib.parse.urlparse(self.path).path.strip("/").split("/")
        if len(parts) == 3 and parts[0] == "super" and parts[1].isdigit():
            return int(parts[1]), parts[2]
        return None

    def _pr_route(self) -> tuple[str, int, str] | None:
        """`/pr/<owner>/<repo>/<n>/<action>` — the per-PR card's buttons."""
        parts = urllib.parse.urlparse(self.path).path.strip("/").split("/")
        if len(parts) == 5 and parts[0] == "pr" and parts[3].isdigit():
            return f"{parts[1]}/{parts[2]}", int(parts[3]), parts[4]
        return None

    # -- verbs -----------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler's spelling
        path = urllib.parse.urlparse(self.path).path
        if path == "/health":
            # The pid is here so `crux serve --restart` can stop exactly this
            # process and nothing else: the service names itself and its pid in
            # one answer, so a restart never has to guess from a process list.
            body = json.dumps({"ok": True, "service": "crux",
                               "pid": os.getpid()}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        pr_route = self._pr_route()
        if pr_route is not None:
            slug, pr, action = pr_route
            self._send(pr_page(slug, pr) if action in ("merge", "admin-merge")
                       else _page("Crux", "<p>Nothing here.</p>"),
                       200 if action in ("merge", "admin-merge") else 404)
            return
        route = self._route()
        if route is None:
            self._send(_page("Crux", "<h1>Crux</h1><p>Nothing here.</p>"), 404)
            return
        if pr_route is not None:
            slug, pr, action = pr_route
            if action in ("merge", "admin-merge"):
                self._send(do_pr_merge(slug, pr, admin=action == "admin-merge"))
            else:
                self._send(_page("Crux", "<p>Nothing here.</p>"), 404)
            return

        number, action = route
        bundle = self._bundle(number)
        if bundle is None:
            self._send(_page("Crux", (
                f"<h1>Super PR #{number}</h1><p class=stop>Crux could not find "
                f"the brief for super PR #{number}. Check that "
                f"<code>[super] home</code> in your crux.toml names the repo "
                f"its brief is filed in, and that <code>gh</code> can read "
                f"that repo.</p>")), 404)
            return
        if action == "merge":
            self._send(merge_page(bundle, self.cfg))
        elif action == "checkout":
            self._send(checkout_page(bundle))
        elif action == "close":
            self._send(close_page(bundle))
        else:
            self._send(_page("Crux", "<h1>Crux</h1><p>Nothing here.</p>"), 404)

    def do_POST(self) -> None:  # noqa: N802
        route = self._route()
        pr_route = self._pr_route()
        if route is None and pr_route is None:
            self._send(_page("Crux", "<p>Nothing here.</p>"), 404)
            return
        # A form POST from another origin is the one remote-ish attack a
        # loopback port has: browsers happily post cross-site. Both guards are
        # cheap, and either alone would do.
        site = self.headers.get("Sec-Fetch-Site", "")
        if site and site not in ("same-origin", "none"):
            self._send(_page("Crux", "<p class=stop>Refused a cross-site "
                                     "request.</p>"), 403)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        fields = urllib.parse.parse_qs(
            self.rfile.read(min(length, 8192)).decode("utf-8", "replace"))
        if fields.get("token", [""])[0] != _TOKEN:
            self._send(_page("Crux", (
                "<p class=stop>This page was made by an older <code>crux "
                "serve</code>. Reload it from the brief and press again.</p>"
            )), 403)
            return

        number, action = route
        bundle = self._bundle(number)
        if bundle is None:
            self._send(_page("Crux", (
                f"<p class=stop>Crux could not find the brief for super PR "
                f"#{number} — check <code>[super] home</code> in your "
                f"crux.toml.</p>")), 404)
            return
        if action == "merge":
            self._send(do_merge(bundle, self.cfg))
        elif action == "admin-merge":
            self._send(do_admin_merge(bundle, self.cfg))
        elif action == "checkout":
            self._send(do_checkout(bundle, self.cfg))
        elif action == "close":
            self._send(do_close(bundle, self.cfg,
                                with_prs=bool(fields.get("prs"))))
        elif action == "ask":
            self._send(do_ask(bundle, self.cfg))
        else:
            self._send(_page("Crux", "<p>Nothing here.</p>"), 404)


def _health(port: int, timeout: float = 0.4) -> dict | None:
    """The service's own answer about itself, or None if nothing answered."""
    import urllib.error
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health",
                                    timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError:
        return {}               # something answers HTTP here, just not us
    except (OSError, ValueError):
        return None
    return body if isinstance(body, dict) else {}


def probe(port: int, timeout: float = 0.4) -> str:
    """What is on *port*: "crux", "other", or "free".

    Deliberately more than a TCP connect. Something else holding the port is a
    different situation from nothing holding it — one means "leave it alone",
    the other "start". A connect alone cannot tell them apart.
    """
    body = _health(port, timeout)
    if body is None:
        return "free"
    return "crux" if body.get("service") == "crux" else "other"


def service_pid(port: int) -> int | None:
    """The pid of the crux service on *port*, if a crux service is there."""
    body = _health(port)
    if not body or body.get("service") != "crux":
        return None
    pid = body.get("pid")
    return pid if isinstance(pid, int) and pid > 0 else None


def stop(port: int, timeout: float = 5.0) -> str:
    """Stop the crux service on *port*. Returns what it did: "stopped",
    "free", "other", "unknown", or "failed".

    "unknown" is the service answering as crux but naming no pid, which means
    exactly one thing: it is an older Crux, started before /health carried one.
    It is the first restart after this change and no other case — and it is
    worth its own answer, because "I cannot identify it" and "I tried and it
    would not die" call for different things from the person reading.

    Asks the port who it is before signalling anything, and signals only a pid
    that answered "crux" on this machine. Killing by name would be the obvious
    shortcut and the wrong one: `pkill -f "crux serve"` on a developer's box
    also takes out the editor session with that string in its argv, and there
    is no portable process-list to match against anyway.

    Escalates once. SIGTERM is enough for an HTTP server with nothing to flush,
    but a wedged process that keeps the port is exactly the thing a restart
    exists to clear, so an unresponsive one is killed rather than reported as a
    puzzle. On Windows there is no SIGKILL and `os.kill` is already a hard
    terminate, so the escalation is a no-op there and costs nothing.
    """
    state = probe(port)
    if state != "crux":
        return state if state in ("free", "other") else "failed"
    pid = service_pid(port)
    if pid is None:
        return "unknown"
    hard = getattr(signal, "SIGKILL", signal.SIGTERM)
    for sig in (signal.SIGTERM, hard):
        try:
            os.kill(pid, sig)
        except OSError as exc:  # already gone, or not ours to signal
            log.info("could not signal crux serve (pid %d): %s", pid, exc)
        deadline = time.monotonic() + timeout / 2
        while time.monotonic() < deadline:
            if probe(port) == "free":
                return "stopped"
            time.sleep(0.1)
    return "failed"


def restart(cfg: Config, port: int = 0,
            log_: logging.Logger | None = None) -> str:
    """Stop the service on the port and start a fresh detached one.

    This exists because the service is the one part of Crux that outlives the
    command that started it. Every other subcommand runs the code that is on
    disk right now; `crux serve` keeps whatever was on disk when it started,
    which is the wrong code the moment anyone edits `crux/serve.py` — the
    normal case for anyone developing Crux, and invisible from the outside,
    because the stale service answers perfectly well.

    Returns "restarted", "started", "other", "disabled", or "failed". Detaching
    (rather than running in the foreground) is the point: this is meant to be
    typed after an edit and to leave a service behind that survives the
    terminal, exactly like the autostart's.
    """
    log_ = log_ or log
    port = port or cfg.serve_port or 8787
    if not port:
        return "disabled"
    stopped = stop(port)
    if stopped == "other":
        log_.warning("port %d is held by something that is not crux; leaving "
                     "it alone", port)
        return "other"
    if stopped in ("failed", "unknown"):
        log_.warning("crux serve on port %d would not stop (%s)", port, stopped)
        return stopped
    try:
        from crux.cli import _spawn_detached
        _spawn_detached(["serve", "--port", str(port)], log_)
    except Exception as exc:  # noqa: BLE001 — same never-raise contract as above
        log_.info("could not start crux serve: %s", exc)
        return "failed"
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if probe(port) == "crux":
            return "restarted" if stopped == "stopped" else "started"
        time.sleep(0.1)
    return "failed"


def ensure_running(cfg: Config, log_: logging.Logger | None = None) -> str:
    """Start the service if it is not up. Returns what it found: the D38 autostart.

    Crux's buttons are printed into every card and brief the moment
    `[serve] port` is set, so a service nobody remembered to start is a page of
    dead links — the failure lands on the reader, who did nothing wrong. This
    runs wherever Crux has just published something with buttons in it, and
    wherever a push happens, which between them means the service is up on any
    machine actually using Crux, and comes back by itself after a reboot.

    Deliberately not a system service: Crux's hooks are OS-agnostic sh shims
    that hand off to Python precisely so nothing depends on systemd, launchd or
    Task Scheduler, and an autostart that only worked on two of the three
    platforms would be a worse promise than none. This reuses the same detach
    the background review already uses.

    Never raises, and never fights for the port: a foreign listener is left
    exactly where it is, with one log line saying so.
    """
    log_ = log_ or log
    port = cfg.serve_port
    if not port:
        return "disabled"
    state = probe(port)
    if state == "crux":
        return state
    if state == "other":
        log_.warning("port %d is held by something that is not crux; the "
                     "buttons on Crux's cards will not work until [serve] port "
                     "names a free port", port)
        return state
    try:
        from crux.cli import _spawn_detached
        # The port goes on the command line, not left to the child: a detached
        # process resolves config from ITS cwd, which is not always the repo
        # whose config we just read — and a service listening somewhere other
        # than the port printed into the cards is the same dead link.
        _spawn_detached(["serve", "--port", str(port)], log_)
        log_.info("started crux serve on 127.0.0.1:%d (D38 autostart)", port)
    except Exception as exc:  # noqa: BLE001 — never break a push over this
        log_.info("could not start crux serve: %s", exc)
    return state


def serve(cfg: Config, port: int = 0) -> None:
    """Run the loopback service until interrupted."""
    port = port or cfg.serve_port or 8787
    # Started from a hook AND by hand is the normal case, so losing the race is
    # not an error: the winner is already serving the same thing.
    if probe(port) == "crux":
        log.info("crux serve is already running on 127.0.0.1:%d", port)
        print(f"crux serve is already running on http://127.0.0.1:{port}")
        return
    Handler.cfg = cfg
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    log.info("crux serve listening on http://127.0.0.1:%d", port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


def start_background(cfg: Config, port: int = 0) -> tuple[object, int]:
    """Start the service on a thread and return (server, port). For tests."""
    Handler.cfg = cfg
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, httpd.server_address[1]
