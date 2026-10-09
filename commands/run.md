---
description: Analyze the current branch with Crux and post/update the sticky review card on its PR
---

Run `crux run` in the repository root. It analyzes the branch, posts or
updates the single sticky Crux card on the branch's PR, and announces to
Slack when a channel is configured. Pass through any arguments the user
gave (e.g. `--pr N` to target a specific PR).

Note: pushes and PR creations from this session already trigger this
automatically via the plugin's PostToolUse hook — only run it manually when
the user explicitly asks, or the automatic run evidently didn't happen (then
also check `~/.cache/crux/crux.log` for why).

**If posting fails on a gh-less host** (claude.ai/code cloud sessions: the
egress proxy answers GitHub API reads with its own credential but does not
pass raw writes through, so neither `gh` nor Crux's REST fallback can post),
fall back to posting the card yourself:

1. Run `crux preview` and capture the full rendered card, starting at the
   `<!-- crux:card -->` marker line.
2. Find the PR's existing comment containing `<!-- crux:card -->` (GitHub
   MCP: `pull_request_read`/`get_comments`). If one exists, update it with
   the new card body; otherwise create a new PR comment with the card as its
   body (`add_issue_comment`). Never post a second card — one sticky
   comment, updated in place.

$ARGUMENTS
