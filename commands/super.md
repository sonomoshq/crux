---
description: Bundle related PRs across repos into one super PR — a single brief, and one call to merge them all
---

Run `crux super` for the user. A super PR (D37) takes a feature that spans
several repositories and treats it as ONE change: one brief covering all of
it, and one command that merges every member PR.

Pick the verb from what the user asked for:

- **"make a super PR", "bundle these"** → `crux super new`. With no picker
  numbers this prompts interactively; you generally want to run
  `crux super new` with no numbers first, SHOW the user the numbered
  candidate list, and let them choose. Only pass numbers yourself when the
  user already named which ones they want (`crux super new 1 3-5`). Add
  `--name "..."` when the user gave the bundle a name; otherwise it takes the
  first selected branch.

  Selected branches that have no PR yet get one opened. The command prints
  `owner/repo` and branch for each before pushing anything, and asks once on a
  terminal (`--yes` skips it). Run from here there is no terminal, so it goes
  ahead — if any picked branch has no PR, confirm that with the user IN CHAT
  before you pass the numbers, naming the repos.
- **"add this repo too", "I forgot one"** → `crux super add <n>`. Same picker,
  same rules about opening PRs as `new` above — run it with no numbers first,
  show the list, let them choose. The list never offers a PR that any open
  bundle already holds, so what it shows IS what can be added. Reach for this
  rather than rebuilding the bundle: the super PR keeps its number, its brief
  and its Slack thread, and every link anyone has posted keeps working.
- **"take this one out", "that repo does not belong"** → `crux super remove
  <n>`, choosing from the bundle's OWN members (a different list from `add`'s).
  Say plainly what it does when you report it: the pull request is untouched —
  not closed, not commented on — and simply stops being part of the super PR.
  A member that already merged stays, because the brief is the record of what
  landed as one change. Removing the last one is refused; that is `close`.
- **"what super PRs do I have"** → `crux super list`.
- **"show me super PR 3"** → `crux super show 3`.
- **"re-run it", "the PRs changed"** → `crux super refresh <n>`.
- **"merge it"** → `crux super merge <n>`. This approves every member PR in the
  user's name and lands them, so unless they have clearly just asked for the
  merge, show `crux super show <n>` first and confirm. Add `--yes` only once
  they have confirmed.

  If they wrote EVERY PR in the bundle it refuses outright — that is the rule,
  not a bug: GitHub will not take their approval on their own work. Do NOT
  merge the PRs individually with `gh` to get around it. Offer
  `crux super ask <n>`, which asks in the bundle's Slack thread for someone
  else. If they wrote only some, it lands the rest and skips theirs with a
  reason; report which one is waiting and for whom.

  `--admin` merges on admin rights without approving anything, and records the
  override on the brief and in Slack. Use it only when the user asks for it.

  It lands in the bundle's landing order with the bundle's merge method (see
  `order` below). `--method` overrides the method for this one run only.
- **"land B before A", "they have to go in this order", "use merge commits"**
  → `crux super order <n>` (D41). With no arguments it prints the current
  landing order, the merge method, and the command that would pin that order —
  show it to the user and edit THAT. To pin, list every member still to land,
  in order: `crux super order <n> owner/repo#N repo#N …` (`repo#N` is enough
  when the repo name is unique in the bundle). A partial list is refused on
  purpose; the error prints the full command to paste. `--method
  merge|squash|rebase` sets how the bundle merges for everyone who presses
  Merge (`default` drops it back to `[super] merge_method`), and `--unpin`
  hands the order back to the review pass. A pinned order survives every
  re-brief, including the automatic one after a push; members added later go
  last. The brief is updated in place without a model call — no `--no-llm`
  refresh needed, and do NOT run one to "apply" it (that would throw away the
  brief's analysis).

  Suggest `--method merge` when the member PRs are stacked or built on each
  other's commits: after a squash merge the later PRs conflict with the very
  commits they contain.
- **"let me test it", "check these out"** → `crux super checkout <n>`. Puts
  every member repo on its PR branch and prints the verification steps. Repos
  with uncommitted changes are skipped and named — tell the user which ones and
  let them decide, never stash or discard for them.
- **"close it", "abandon it"** → `crux super close <n>`. Add `--prs` only if
  they explicitly want the member pull requests closed too — that discards open
  work in several repos, possibly other people's.

Use `--dry-run` on `new`/`add`/`remove`/`refresh` to print the brief without
filing the issue — do that whenever the user wants to see it before it goes
public. A dry run saves no bundle, pushes no branch and opens no PR, so a picked
branch without a PR is left out of the preview (the output names it).

Running from here there is no terminal, so an interactive picker gets no
answer and cancels ("no answer on stdin") — pass the picker numbers instead.

A super PR is ONE change: one analysis pass over the combined diff, one Slack
message, one set of prompts. Do not run `crux run`/`crux preview` on the member
branches as well — not before bundling, not after — and do not offer to. A push
to a member branch re-briefs the bundle by itself.

## The brief's buttons

The published brief carries Merge, Set-up-to-test and Close links pointing at
`http://127.0.0.1:8787`, served by `crux serve` on the reader's own machine —
that is how the approval carries their name. If the user asks why a button does
nothing, they are not running `crux serve`; tell them to start it. If they ask
why the Merge button refused them, they wrote part of the bundle (see above).

## What to tell the user afterwards

For `new`/`refresh`, give them the issue URL and the headline finding — a
merge conflict between two member PRs, or the landing order — not a
restatement of the brief. The brief is one screen and they can read it.

For `merge`, report exactly what landed and what did not. The command merges
everything it can and reports the rest rather than stopping at the first
failure, so a partial result is normal and the blocked PRs each carry a
reason. Re-running after fixing a blocker is safe: merged PRs are skipped.

## Setup

One key in crux.toml — the repo that holds the brief issues:

```toml
[super]
home  = "your-org/pr-bundles"    # repo that holds the brief issues
roots = ["~/src"]        # where local clones are found (optional)
```

There is no separate list of bundleable repos. Candidates come from the repos
already in `[scope]` — the `repos` allowlist when set, else every repo of
`owners` — as local branches plus their open PRs. A branch with no PR gets one
opened when selected. A PR already in another super PR is never offered twice.

If `crux super new` reports no home repo, `[super] home` is unset: tell the
user which repo to name rather than picking one for them.

$ARGUMENTS
