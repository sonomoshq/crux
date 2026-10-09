---
description: Link Zenhub tickets to a PR or super PR with Crux, and close them when the work lands
---

Run `crux zenhub` for the user. It links the Zenhub tickets a pull request
closes and closes them when the PR lands — including across repos, where
GitHub's own `Closes #N` does not reach.

**Check it is on first.** `crux zenhub status` says so in one line. If it
reports Zenhub is off, tell the user what is missing (a `[zenhub] workspace`
in crux.toml, an API key, or both) and stop — do NOT run `crux zenhub setup`
yourself: it asks for a secret on a terminal you do not have.

Pick the verb from what the user asked for:

Note that the ordinary way a ticket gets linked is the **pre-push** question,
which fires on `git push` when a human is at the terminal. Everything below is
for linking outside that flow — a PR Crux did not open, a wrong answer, or a
push made where there was no terminal to ask on.

- **"link this to ticket N", "this closes ZH-29"** → `crux zenhub link`.
  Run it with `--issue N` when the user named the ticket. With no `--issue` it
  shows a ranked list and prompts on a terminal — which you do not have, so it
  will link nothing. Instead run `crux zenhub link --issue <N>` once the user
  has named the ticket, or show them the candidates yourself and ask IN CHAT
  which one they mean.
- **"link the super PR"** → `crux zenhub link --super <n>`. A bundle's tickets
  hang off the BUNDLE and close only when every member PR has landed. If
  `crux zenhub link` refuses because the branch belongs to an open bundle, that
  is the guard working — use `--super` as it tells you, do not work around it.
- **"what is linked?"** → `crux zenhub list` (read-only).
- **"the ticket is still open but the PR merged"** → `crux zenhub sync`. Crux
  has no webhook, so a PR merged in the GitHub UI, by a teammate, or by
  automerge needs this. Run `crux zenhub sync --dry-run` first and show the
  user what it would close before running it for real.
- **"nothing is happening", "it links but never closes"** → `crux zenhub
  doctor`. It introspects the live Zenhub schema and reports which calls the
  server accepts. A ⚠️ on `createIssuePrConnection` is expected and harmless —
  linking and closing both still work.
- **"unlink", "wrong ticket"** → `crux zenhub unlink` (add `--super <n>` for a
  bundle).

Linking and syncing write to GitHub and Zenhub, so say what you are about to
do before you do it. `list`, `status`, `doctor` and `--dry-run` are read-only.
If something fails, check `~/.cache/crux/crux.log` — every Zenhub call logs
its own failure there rather than raising.

$ARGUMENTS
