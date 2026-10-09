---
description: List open PRs across your repos with Crux (read-only) — the current repo, named repos, or all repos of your scope owners
---

Run `crux prs` in the repository root and show the user the aligned list of
open pull requests. This is read-only — one `gh pr list` per repo, fanned out
in parallel — and posts nothing to GitHub or Slack. Pass through any arguments
the user gave: `.` or a path for the current repo, `owner/name` (or bare names
resolved against `[scope] owners`) for specific repos, or `--jobs N` to size
the parallel fan-out.

With no arguments it lists every repo of the `[scope] owners` in crux.toml.
If it fails, confirm `gh auth status` is authenticated and `[scope] owners`
is set, then check `~/.cache/crux/crux.log` for the failing repo.

$ARGUMENTS
