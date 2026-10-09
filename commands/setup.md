---
description: Set up Crux on this machine - CLI install, git hooks, repo scope, super PRs, and Slack
---

Walk the user through setting up Crux, verifying each step before moving on:

1. **CLI**: `crux --version`. If missing: `pipx install git+https://github.com/sonomoshq/crux`
   (or `pipx install .` from a clone; in cloud sessions the plugin's
   SessionStart hook installs it automatically — `pip install --user` from the
   repo is the manual fallback).
2. **Prerequisites**: `git`, `rg`, `gh auth status` (authenticated), and the
   `claude` CLI logged in. On gh-less hosts posting uses the GitHub REST API
   with a `GH_TOKEN`/`GITHUB_TOKEN` env var (never pasted into the chat) —
   except claude.ai/code cloud sessions, where raw API writes are blocked by
   the egress proxy and the card is posted via the session's GitHub MCP
   tools instead (see `/crux:run`); no token needed there.
3. **Hooks**: `crux install-hooks` (global; `--local` for just this repo).
   This wires the pre-push auto-review and intent capture for pushes made
   OUTSIDE Claude Code sessions too. The plugin's own hooks already cover
   pushes made inside sessions and will not double-review.
4. **Scope — which repos Crux acts on**: write `~/.config/crux/crux.toml`
   (or the repo's `crux.toml`):

   ```toml
   [scope]
   owners = ["<github-owner>"]
   repos  = ["<owner>/<repo>", ...]   # optional; omit to allow all of owners'
   ```

5. **Super PRs (optional)**: for features that span several repos. Needs one
   thing in crux.toml — a repo to hold the brief issues:

   ```toml
   [super]
   home  = "<owner>/pr-bundles"   # must EXIST; the brief is filed there as an issue
   roots = ["~/src/<org>"]       # where local clones live (optional)
   ```

   `python install.py` prompts for `home`, offers to create the repo, and adds
   the block to a config written before super PRs existed. The bundleable
   repos are the `[scope]` ones from step 4 — there is no second list to
   maintain. Verify with `crux super new` to see the candidate list.

6. **The card buttons (nothing to do)**: Crux's Merge / Set-up-to-test / Close
   links call `crux serve` on `127.0.0.1:8787`, and it starts itself — on every
   push and every card or brief published, detached, on any OS. It must run as
   the person clicking, which is what lets an approval carry their name. Only
   mention it if a button reports it cannot connect (then: `crux serve`), or if
   they want it off — `[serve] port = 0` removes the links from the cards.
7. **Slack (optional)**: set `[slack] channel` in crux.toml to the channel ID
   (or name, which also needs the `channels:read` bot scope). For the token,
   have the user either export it as `SLACK_BOT_TOKEN` where crux runs, or
   run `python install.py` from a Crux clone which saves it to
   `~/.config/crux/credentials.json` (0600). NEVER ask them to paste the
   token into the chat, and never write it into a repo file.
8. **Verify**: `crux preview` on a branch with real changes.

$ARGUMENTS
