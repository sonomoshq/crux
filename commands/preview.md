---
description: Analyze the current branch with Crux and print the review card without posting anything
---

Run `crux preview` in the repository root and show the user the rendered
card. This runs the full Crux analysis (diff, signals, LLM pass) but posts
nothing to GitHub or Slack.

If the command is not on PATH or fails, consult the crux skill's
prerequisites section (git, rg, claude login, pipx install) and help the user
fix the specific missing piece — check `~/.cache/crux/crux.log` for the
failure before guessing. `gh` is NOT among them: preview posts nothing, so it
needs no GitHub auth and works on a gh-less host (cloud sessions included).

$ARGUMENTS
