---
name: crux
description: Analyze the current branch's pull request with Crux and post or preview its review card. Use when the user asks to review a PR with Crux, run crux, preview a review card, set up Crux, or asks why a Crux card did or didn't appear after a push or PR creation.
---

# Crux: PR review cards

Crux finds the crux of a PR: it reads the branch's diff, skips trivial
changes, and posts ONE self-updating comment on the PR — the lines a reviewer
actually needs to read, in causal order, with evidence, plus proof the rest
is safe to skim. If a Slack channel is configured, it also announces the PR
there (first push posts a message; later pushes reply in its thread).

## Commands

All analysis happens through the `crux` CLI (it must be installed and this
must be a git repo with a GitHub origin):

- `crux preview` — run the full analysis, print the card to the terminal,
  post nothing. Use this when the user wants to see the review before it
  goes public, or there is no PR yet.
- `crux run` — analyze and post/update the sticky PR card (and the Slack
  announcement when configured). Use `crux run --pr N` to target a specific
  PR number.
- `crux merge` — approve this branch's PR in the user's name and merge it.
  `--pr N` targets another PR, `--admin` merges on admin rights without
  approving (recorded as a comment on the PR), `--yes` skips the confirmation.
  The user cannot approve their own PR — that refusal is the rule, not a bug,
  and merging it another way with `gh` is not the workaround. A branch inside a
  super PR is refused with a pointer to `crux super merge <n>`; use that.
- `crux install-hooks` — install the git hooks (pre-push auto-review,
  post-commit message enrichment, post-checkout base tracking) and the
  Claude Code Stop and SessionStart hooks, once per machine (`--local` for one repo).
- `crux super …` — super PRs (D37): brief and merge a feature that spans
  several repos as ONE change. See below.

## Super PRs (D37)

When a feature spans repositories, reviewing its PRs one at a time hides the
thing that matters: what they add up to, and what breaks in the gaps between
them. `crux super` bundles them.

- `crux super new [N ...] [--name TEXT]` — show the numbered candidate list
  (local branches plus open PRs across the `[scope]` repos, newest first) and
  create a super PR from the chosen ones. Branches with no PR get one opened.
  There is no repo group to name: the PRs you pick ARE the super PR.
- `crux super add <n> [N ...]` — add more PRs to a super PR that already
  exists, from the same numbered list (which never offers a PR any open bundle
  already holds). Use this when a cross-repo change turns out to touch a repo
  nobody expected: it keeps the super PR's number, its brief and its Slack
  thread, which rebuilding it would all lose. Re-briefs automatically.
- `crux super remove <n> [N ...]` — detach PRs from a super PR, choosing from
  its own members. The pull requests themselves are untouched — not closed, not
  commented on — and go back to being ordinary PRs, selectable again. A member
  that already merged stays (the brief is the record of what landed), and the
  last member cannot be removed: use `close` to retire a reading.
- `crux super refresh <n>` — re-brief the bundle after its PRs move. A push to
  any member branch does this automatically (the pre-push and Claude auto-run
  hooks detach it instead of the per-PR `crux run`).
- `crux super merge <n>` — approve every member PR as the person running it,
  then merge, reporting per-PR reasons for anything blocked.
- `crux super order <n> [REF ...] [--unpin] [--method merge|squash|rebase|default]`
  (D41) — pin the landing order and/or the merge method. With no REFs and no
  flags it just shows the current order and method. REFs are `owner/repo#N` or
  `repo#N`, and every member still to land must be listed (no prefixes). Once
  pinned, every re-brief keeps the order — including the automatic one after a
  push — and the brief's Merge button and `crux super merge` follow it and the
  bundle's method on anyone's machine. Reach for `--method merge` when the PRs
  are built on each other's commits (a squash leaves the later ones conflicting).
  It restamps the brief in place: no model call.
- `crux super checkout <n>` — put every member repo on its branch so the
  bundle can be tried by hand, then print the verification steps. Prefer this
  over checking repos out one at a time; it skips repos with uncommitted work
  rather than moving anyone's edits.
- `crux super ask <n>` — ask in Slack for someone else to merge it.
- `crux super close <n> [--prs]` — close the brief, optionally the PRs too.
- `crux super list` / `show <n>`.
- `crux serve` — the loopback service behind the buttons on Crux's cards and
  briefs. It starts ITSELF (on every push, and whenever a card or brief is
  published), so do not run it routinely or tell the user to. Run it only if a
  button reports it cannot connect.

**You cannot approve your own work** (D38), at two levels:

- The user wrote EVERY member PR → `crux super merge` refuses outright. Do not
  work around it by merging the PRs one at a time with `gh`. Offer
  `crux super ask <n>`, which posts in the bundle's Slack thread asking for
  someone else, or `--admin` if they have admin rights (see below).
- The user wrote SOME of them → it approves and lands the rest, skips theirs
  with a loud per-PR reason, and reports it. A partial result there is correct,
  not a failure; tell them which PR is waiting for someone else.

`crux super merge <n> --admin` merges on admin rights without approving
anything — the escape hatch when the approval path cannot be satisfied. It
needs admin on every member repo, and it stamps who overrode, and that nothing
was approved, into the merge report and the Slack reply. Do not reach for it to
get past the author rule unless the user asks for it by name.

The ordinary per-PR card carries the same **Approve and merge** button, with
the same rule and the same admin override.

Two properties to preserve when working on this, because both are easy to
break and are the point of the feature:

1. **One analysis pass.** A super PR over 8 PRs makes ONE model call, not
   nine. The combined diff is computed by merging the PR heads in memory
   (`git merge-tree`, which never touches the working tree), harvested per
   repo for free, then analyzed once. Never "run crux on each PR and
   summarize" — that costs an order of magnitude more and still cannot see
   cross-repo seams.
2. **One screen.** The brief is the same length for 3 PRs and 30. Caps are
   enforced in `crux/superrender.py`, not just requested in the prompt.
3. **Nothing per repo.** A super PR analyzes once, announces to Slack once, and
   asks its own questions. Selecting branches that have no PR yet pushes them,
   and `crux super` sets `CRUX_SUPER=1` on those pushes so the pre-push hook
   stands down — no per-repo review card, no per-repo Slack message, no
   repo-less "Create a PR for `<branch>`?" prompt. Never run `crux run` or
   `crux preview` on a member branch to "also" review it, and never suggest it:
   that is the N+1 the feature exists to abolish.

The brief is filed as a sticky issue in the `[super] home` repo (GitHub has no
repo-less issue and no cross-repo grouping — stacked PRs are single-repo), and
each member PR gets a one-line pointer back to it. Merge policy is land-what-
can: a cross-repo merge cannot be atomic, so it merges everything it can and
names what blocked, rather than pretending to roll back across repos.

When Slack is configured it is announced there as well, with every refresh and
the merge report threaded under the original message.

```toml
[super]
home  = "your-org/pr-bundles"
roots = ["~/src"]
```

Both `preview` and `run` need: `git`, ripgrep (`rg`), and the `claude` CLI
logged in (`claude login`). `run` additionally needs a way to POST — `gh`
(authenticated — `gh auth status`) on a normal host; see the next paragraph
for hosts without it. If a command fails, check those first, then
`~/.cache/crux/crux.log`.

On hosts without `gh`, posting falls back to the GitHub REST API
authenticated by `GH_TOKEN` or `GITHUB_TOKEN` (never pasted into the chat).
claude.ai/code cloud sessions are more restricted still: the egress proxy
serves GitHub API *reads* with its own credential but does not pass raw
*writes* through, so direct posting fails there no matter the token. In that
case run `crux preview` and post/update the sticky card yourself through the
GitHub MCP tools, exactly as described in the `/crux:run` command (one
comment carrying `<!-- crux:card -->`, updated in place — never a second
card). `crux preview` always works with no token. Two things have no REST
fallback and need `gh` on every host: `crux prs`, and the repo clone inside
`crux super` — neither is available on such hosts.

Note the failure is not always a *posting* failure. With no token at all,
`crux run` stops at PR discovery, before any analysis; with a token, the
analysis runs and the card upsert is what fails. In both cases `crux preview`
still works, and the card has to be posted from the session.

## Automatic activation

This plugin ships a PostToolUse hook that watches for `git push`,
`gh pr create`, and the GitHub MCP push/PR tools, and detaches a `crux run`
automatically — so a PR shipped from any Claude Code session (terminal,
desktop app, or a claude.ai/code cloud session) gets its review card without
being asked. Do not also run `crux run` manually right after pushing unless
the user asks; the hook already handles it.

Which repositories it acts on is controlled by crux.toml:

```toml
[scope]
owners = ["your-org"]                 # only repos of these GitHub owners (D13)
repos  = ["sonomoshq/crux"]         # optional: only these exact repos
```

Global config lives at `~/.config/crux/crux.toml`; a repo's own `crux.toml`
overrides it key by key. If a card didn't appear, the repo is probably out of
scope, the change was gated as trivial, or a prerequisite above is missing.

## Slack

Announcements are configured with `[slack] channel` in crux.toml. The bot
token is NEVER stored in config or the repo: Crux reads it from the env var
named by `[slack] token_env` (default `SLACK_BOT_TOKEN`) or from the
credentials file `~/.config/crux/credentials.json` (`python install.py` can
write it, chmod 0600). The bot needs `chat:write` (+ `channels:read` if the
channel is configured by name rather than ID) and must be invited to the
channel. Never ask the user to paste the token into the conversation; point
them at the credentials file or env var instead.

## Installing the CLI

The plugin provides commands/hooks, but the `crux` CLI itself is a Python
package. If `crux` is not on PATH:

```sh
pipx install git+https://github.com/sonomoshq/crux   # or, from a clone: pipx install .
```

In a claude.ai/code cloud session there is normally nothing to do: the
plugin's SessionStart hook pip-installs the CLI from the plugin's own checkout
when it is missing. Manual fallback:
`pip install --user git+https://github.com/sonomoshq/crux`.
