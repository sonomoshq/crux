# Crux — Design (approved 2026-07-04)

**Pitch:** A local tool that finds the crux of a PR. Trivial PRs it skips entirely. Substantial
ones get one self-updating GitHub comment telling the reviewer: the ~90 lines you actually need
to read, in the order the change causally happened, with machine-checkable evidence for why each
one matters — and proof that the rest is safe to skim or skip.

Built for teams whose PRs are largely written by coding agents. AI summaries are too shallow,
reading every line is too slow; Crux is the middle: importance-ranked, evidence-backed, causally
ordered.

## Locked decisions

- **D1 — Name:** Crux. CLI command `crux`, Python package `crux`.
- **D2 — GitHub only**, via the `gh` CLI (author's own auth) where it exists, else the
  GitHub REST API directly (`crux.ghrest`); in a claude.ai/code session, via the session's
  own GitHub MCP tools. See D12. No GitLab, no hosted anything.
- **D3 — Line links, both styles:** every `file:line` reference renders as a **permalink**
  (primary): `https://github.com/{owner}/{repo}/blob/{head_sha}/{path}#L{a}-L{b}`, plus a
  secondary `[diff]` link into the PR Files tab:
  `https://github.com/{owner}/{repo}/pull/{n}/files#diff-{sha256hex(path)}R{a}`.
- **D4 — Badges:** `CODE CHANGE` (the root/heart of the change — scrutinize),
  `DESIGN DECISION` (a judgment call — agree or object), `CODE CHANGE EFFECTS`
  (ripples of the core change — check consistency), `MECHANICAL CHANGES` (repeated/generated
  churn — glance at one). Badges appear **only in the item list, never inside the DAG diagram**.
- **D5 — DAG in the card:** a Mermaid `graph LR` of clean nodes — label = `N · short name`
  (e.g. `1 · WriteBuffer class`), edges = "had to change because of". **Shared numbering**:
  DAG node numbers ARE the item numbers below; number + name travel together everywhere so the
  reader never decodes an opaque reference. (**Superseded by D33** for the diagram itself —
  the card's map is now the review's user-level flow, not the DAG. Shared numbering still
  governs the review lists.)
- **D6 — Triviality gate:** decided from deterministic signals only, **before any LLM spend**.
  Skipped PRs get nothing posted.
- **D7 — Local-only:** runs on the author's machine in the background, headless Claude Code
  (`claude -p`) on the existing subscription. Never a metered API key. Never blocks a push.
- **D8 — Sticky comment:** one comment per PR, found via hidden marker `<!-- crux:card -->`,
  edited in place on every run. Header always shows the analyzed head SHA.
- **D9 — Incremental:** per-branch state cache; the previous run's DAG/claims/prose are fed to
  the next LLM pass so only changed parts are re-analyzed. Merge-base change (rebase/force-push)
  invalidates everything.
- **D10 — Intent capture:** a Claude Code Stop hook snapshots the authoring session's intent /
  decisions / uncertainties to `.crux/intent.json` in the working repo. Consumed as ground truth
  and cross-examined (claims audit), never trusted blindly.
- **D11 — PR auto-create:** if no PR exists for the pushed branch, Crux asks in the terminal
  (via `/dev/tty`): create a PR into `main`? — target editable at the prompt, default from
  config. On yes: `gh pr create`. Non-interactive contexts skip the ask and log it.
- **D12 — Stack:** Python ≥3.11 (the floor `pyproject.toml` declares; needs `tomllib`,
  and claude.ai/code containers ship 3.11), **stdlib only**. `rg` required (fallback
  `git grep`), `difft` optional (fallback path/regex classification). Posting prefers `gh`;
  without it, `crux.ghrest` posts straight to the REST API with `GH_TOKEN`/`GITHUB_TOKEN`, and
  in a claude.ai/code session (where the egress proxy blocks API writes whatever the token) the
  Claude Code session posts the card itself through its GitHub MCP tools. Preview mode needs
  none of the three. `crux prs` and `crux super`'s repo clone are still `gh`-only.
- **D15 — Plain English:** everything reviewer-facing must be readable by someone new to the
  codebase, in one pass, without tool jargon. Concretely: the card never says "hunk", "DAG",
  "entailment", "topological", "blast radius", or "fingerprint" — it says "changed chunk",
  "change map", "exists because of", "reading order", "called from N places". Titles state
  what changed as one plain sentence. WHY paragraphs are at most 3 short sentences a new hire
  could follow. Questions are concrete checks that name the line ("If the database write
  fails, are buffered events retried or silently dropped? — line 61"). Chips use words, not
  abbreviations. `crux/prompts/analyze.md` must state this contract explicitly, and every example
  in this document must model it.
- **D14 — Design & standards findings:** the LLM pass also reports OO-design and
  coding-standards findings: single-responsibility drift, inheritance-where-composition-fits,
  leaked internals, **duplicate helpers** (new code re-implementing an existing repo utility),
  and repo-convention violations. Conventions come from the target repo's `CLAUDE.md` (read and
  included in the prompt when present) plus `crux.toml [standards] rules`. **Evidence contract:**
  a finding must cite the offending lines AND its convention source (the CLAUDE.md/crux.toml rule
  text, or file:line of the sibling code / existing helper it diverges from) — the validator
  drops citation-less findings. Rendering: a finding whose file overlaps a RED item renders
  inside that item as a `🧭` line; the rest go in a `### 🧭 Design notes` section. Hard cap
  `standards_max` (default 5). Findings NEVER affect tiers — they inform, they don't gate.
- **D13 — Org allowlist:** Crux only operates on repos whose `origin` owner is in
  `crux.toml [scope] owners` (no default — empty means Crux does nothing). Any other repo ⇒ exit 0 silently with
  a single log line, before any analysis or gh call. Keeps globally-installed hooks inert
  outside company repos.

### Amendments (2026-07-04)

- **D16 — Slack announcements:** with `[slack] channel` set — typically once machine-wide in
  the global `~/.config/crux/crux.toml`, so it covers every repo; a repo's own `crux.toml` can
  pick a different channel or opt out with `channel = ""` — and a bot token resolved by
  `slack._token` (the env var named by `[slack] token_env`, default `SLACK_BOT_TOKEN`, else the
  shell-independent credentials file `~/.config/crux/credentials.json` written by `crux.credentials`;
  scopes `chat:write`, `channels:history`, `channels:read`), Crux announces each PR to the channel. It **checks the
  channel first**: scans recent history for the PR URL and, if already linked (by Crux or a
  human), replies in that message's **thread**; only posts a new top-level message otherwise.
  The message ts is saved in `RunState` as a fallback when the link has scrolled out of history.
  The copy never says Crux reviewed anything — a person reviews; Crux only announces the PR.
  Runs on both the reviewed and trivial paths. Best-effort — never blocks or fails a review.
- **D17 — Diff base is the branch's parent, not `main`:** a `post-checkout` hook silently
  records the branch a new branch was created from as `git config branch.<name>.cruxBase`
  (from git's `branch: Created from` reflog entry, D34). `repo_info` prefers it for the
  merge-base diff and the PR base, falling back to the default branch. So the review covers
  only what the branch changed since it forked. Once a PR exists, its own base wins over
  this guess (D34). A candidate whose merge-base with HEAD *is* HEAD is not a base — it
  already contains the work — so it is skipped rather than accepted, and a branch is never
  its own parent: `git checkout -b feature origin/feature` records "Created from
  origin/feature", which names the branch itself, and taking either at face value made
  `crux preview` on an ordinary clone silently empty. When nothing better resolves, the
  empty diff is logged as a warning rather than passed off as "nothing to flag".
- **D18 — Review is gated on a PR; PR creation is a convenience:** with no PR, Crux does no
  diff and no analysis. It ensures a PR first (into `crux_base`, prompted or auto via
  `[pr] auto_create`), then the D6 gate decides whether that PR gets a review card.
- **D19 — Self-maintaining PR title + description (fast at first, curated after review):**
  `sync_pr_metadata` runs every push; before any review it sets the title + description from
  the branch's commits (no LLM), re-syncing only when they change. After the review, both are
  upgraded from the annotation: the title to the LLM's concise `pr_title`, and the description
  to the review's `summary` + `overview` — a curated account of what the branch does, with
  trivial commits left out, instead of the raw commit list. Upgrades are one-way: a later
  pre-review sync never downgrades an LLM title or an LLM description (marked
  `<!-- crux:pr-body:llm -->`) back to the commit-based placeholder. The description links both
  sticky comments (review + how-to-test). The hidden `<!-- crux:pr-body -->` /
  `<!-- crux:pr-body:llm -->` markers distinguish the commit-based body from the review-written
  one (human-edit handling is D26). `--no-llm` keeps the commit-based placeholders.
- **D20 — High-level card (supersedes the D5 layout):** the card leads with **The big picture**
  — 2–4 plain-English `overview` ideas, each linked to the code, riskiest flagged `⚠️`. The
  change map is kept but shows only the important changes and is omitted above 10 nodes
  (superseded by D33: the map is now the review's own user-level flow, always small enough
  to draw). Must-read / worth-a-skim are one tight line each (with a D4 badge tag), capped per D30
  (must-read 5, skim 8); safe-to-skip
  is a single machine-checked tally. The verbose per-node writeups, claim audit, and design-notes
  sections were dropped from the card and from the required LLM output (less output ⇒ faster).
- **D21 — Progress is visible on the PR and in the terminal:** a `Crux review` GitHub **status
  check** goes pending the moment the PR is known and resolves to success/failure; the launching
  terminal also gets best-effort `/dev/tty` lines (push received → reviewing → posted) from the
  detached run, which retains the controlling terminal.
- **D22 — How-to-test comment:** when the PR ships hand-verifiable functionality, a second
  sticky comment (`<!-- crux:integration-test -->`) lists minimal end-to-end steps, led by a
  **Prerequisites** line that LINKS to the repo's existing setup docs (README install section,
  etc.) rather than duplicating them.

### Amendments (2026-07-05)

- **D23 — 50-line reading cap:** no card pointer may ask a reviewer to read more than
  `[render] max_read_lines` (default 50) lines of code. A must-read/skim item spanning more
  carries a **breakdown** — a CURATED reading list, never a tiling (chunking 600 lines into
  50-line windows is still 600 lines of reading, verified on a real PR): the 2–4 parts where
  the change's real logic/decisions/risk live, most important first, each one plain analysis
  sentence ending with its own ≤50-line `path:a-b` pointer, closed by ONE pointer-less sentence
  accounting for everything left out; total pointed-at reading ~150 lines regardless of item
  size. Written by the LLM (the prompt marks oversized changes `LARGE` and requires it); the
  no-LLM fallback selects the top chunks by harvest score (≤3 windows) and summarizes the
  remainder in one line. The item's whole-span link stays for navigation; the breakdown is the
  reading work. More analysis, less code to read.
- **D24 — Crux does the before/after:** the card never asks the reviewer to compare versions
  ("compare before/after", "see the diff" are banned in the prompt). For a change that rewrites,
  moves, or re-routes existing behavior, the LLM fills `before`/`after` — one short sentence
  each — and the item renders a `**Before:** … **Now:** …` line, so the difference is understood
  without opening the diff.
- **D25 — Tests ride along:** unit tests that mirror the code they test never outrank it. A
  test-only item is capped at **yellow** — sensitive keywords inside a test file can't force red,
  and the LLM can't promote it there — UNLESS it weakens the suite (deleted assertion, skip
  marker, loosened threshold): that floor stays red. The prompt tells the model to keep matching
  tests out of the overview/nodes and to note new coverage inside the code change's why instead.
  Test files are recognized by directory (`[tests] test_dirs`) OR filename shape (`test_*`,
  `*_test.*`, `*.test.*`, `*.spec.*`, `*_spec.*`, PascalCase `*Test`/`*Tests`/`*Spec`,
  `conftest.py`) — directory matching alone missed suffix-named tests and forced them red on a
  real PR. "Deleted assertion" is judged NET across the whole item, not per changed chunk:
  rewriting a test moves assertions between chunks and must not count as weakening.
- **D26 — Crux keeps owning PR metadata unless told to stop:** supersedes D19's back-off-on-
  human-edit rule. A human editing the PR title or description does NOT stop the sync: the next
  push re-syncs both anyway (commit-based before the review, review-written after; the
  never-downgrade rule stands). The one escape hatch is the explicit `<!-- crux:keep -->` HTML
  comment (case- and whitespace-tolerant) in the description — with it, Crux leaves the title AND
  the body alone. Only the COMMENT form opts out: the bare word in a commit subject or the review
  prose does not, so Crux never locks itself out with its own auto-generated text. Every body Crux
  writes ends with a small visible hint naming the opt-out, so it is discoverable in place on the
  PR (and the hint itself, though it shows the comment, is stripped before the opt-out check).
- **D27 — Human commits get a Crux-written summary pasted below them:** the PR title and
  description are assembled from commit subjects (D19), and human-typed messages are usually
  terse ("fix"). A `post-commit` hook detects a human commit — no `Co-Authored-By: … Claude`
  trailer (Claude Code commits already carry thorough messages) and no prior `Amended-by: Crux`
  trailer — and spawns a detached `crux enrich-commit`: one LLM pass over the commit's own diff
  writes a specific subject plus 0–5 plain-English bullets, and the commit is amended in place.
  The human's message is never rewritten — it stays verbatim on top; Crux's part sits below it
  as a `Crux: <subject>` line + bullets, and the `Amended-by: Crux` trailer marks it done (and
  stops recursion, since the amend re-fires the hook). Everything that builds PR metadata from
  commits reads full messages and prefers the `Crux:` subject over the human line
  (`commitmsg.effective_subject`). The amend is guarded — never when HEAD has moved, anything
  is staged, a rebase/merge/cherry-pick is in flight, the commit is a merge, or the commit is
  already on any remote — and the guards re-run after the slow LLM call, just before amending.
  `[commit] enrich = false` opts a repo (or the machine) out, and the D13 org allowlist applies
  as everywhere: outside `[scope] owners` (empty by default), `enrich-commit` exits silently
  before touching git or the LLM. The foreground hook only greps the message and spawns the
  detached run: it never blocks or fails the commit.
- **D29 — Validation gates close the LLM step:** after the model's reply is parsed (and after
  the D15 jargon retry and the D9 reuse merge), deterministic gates check the annotation against
  the card directives and mechanically REPAIR violations in place — a basic refactor, never a
  second LLM call. Every repair is logged. Gates today: **D25 enforcement** — a `red` suggestion
  on a test-only change is demoted to yellow, and a big-picture bullet whose code pointers all
  land in test files is dropped. New card directives should ship with a matching gate here
  (`analyze.validate_annotation`) so the card is held to them even when the model ignores the
  prompt. Relatedly, the D25 weaken-detector only counts a skip marker when it appears OUTSIDE
  a string literal — a test suite testing skip detection adds `"@unittest.skip"` as fixture
  data, which must not read as a skipped test (field-tested: it forced Crux's own test files
  red).
- **D30 — The card is a ≤5-minute read, enforced (tightens D20's caps):** field-flagged: ~8-min
  cards were routinely too long. The header never claims more than **~5 min**; **Must read**
  shows at most **5** items (overflow becomes one `…and N more — see the diff` line); **Worth a
  skim** entries are STRICTLY one line — headline + link, no why-prose, no before/now, no
  breakdown sub-bullets (sub-bullets on skim items were a top length driver). Breakdowns and
  before/now lines belong to must-read items only. If a card ever genuinely needs more than 5
  minutes, the fix is splitting the PR, not a longer card.
- **D28 — No commit is pushed un-enriched (closes the D27 race):** committing and pushing
  immediately would otherwise send the terse message while the detached enrichment is still
  generating — and the D27 pushed-commit guard would then cancel that enrichment forever. So the
  pre-push hook runs `crux ensure-enriched --hook` in the FOREGROUND, before any refs move,
  feeding it the hook's stdin ref lines. For every pushed branch ref it checks each outgoing
  commit (not on any remote, ≤30 or it backs off); human commits still missing the `Amended-by:
  Crux` trailer are enriched right there, and the whole un-pushed chain is rebuilt with
  `git commit-tree` — message-only rewrites that preserve trees, parents, and authors, touch
  neither index nor working tree, and land via a compare-and-swap `update-ref`. Because that
  (or a detached amend racing in mid-push) makes the shas git already resolved stale, the hook
  then STOPS the push — the one deliberate exception to D7's never-block rule — with
  "run `git push` again"; the retry finds every commit enriched and proceeds with zero extra
  work. Staleness is judged by one test that covers both racers: the pushed sha no longer equals
  the ref. LLM failures never stop a push (the commit goes up terse, logged); `[commit] enrich =
  false` disables this check together with D27.

### Amendments (2026-07-22)

- **D31 — Repo memory (the LLM memory management tool):** Crux keeps one small store of
  durable plain-English facts per repo — conventions, architectural quirks, recurring
  pitfalls — at `$XDG_CONFIG_HOME/crux/memory/<owner>__<repo>.json` (config, not cache:
  knowledge must survive the rebase/force-push cache drops of D9). Every review reads the
  store into its prompt ("What you remember about this repo") so the model judges a PR the
  way a longtime maintainer would, and may propose at most 3 new facts, absorbed only AFTER
  the card posts — `--dry-run`/preview never writes, and `--no-llm` proposes nothing.
  Hygiene is deterministic and lives in ONE write path (`memory.absorb`): dedupe by
  normalized-text hash (which is the id), drop proposals anchored to files that do not
  exist, forget stored facts whose anchor has since vanished, and cap the store
  (`[memory] max`, default 40) evicting review-written facts oldest-first — facts a human
  added via `crux memory add` outlive all of them. `crux memory` (list/add/forget/clear)
  is the management surface. `[memory] enabled = false` stops reviews reading and writing
  the store; the CLI itself keeps working. Like everything else (D7), memory is
  per-machine: two authors each accumulate their own.

### Amendments (2026-07-30)

- **D32 — Every line has to earn its place (tightens D20/D30 on *density*, not just
  length):** D30 capped how MANY entries a card may show; shipped cards were still dense,
  because each entry was long and several said nothing. Four rules, each with its
  enforcement point:
  - **Word budgets, in the prompt** (`prompts/analyze.md`): summary 25 · each overview
    bullet 25 · title 8 · why 20 · before/after 15 each · each breakdown part 20. One
    sentence means one sentence — no semicolons, no parenthetical asides. Prose cannot be
    shortened mechanically without mangling it, so this is prompt-enforced and *linted*
    (`analyze.lint_word_budgets`, logged) rather than repaired like a D29 gate; the lint is
    how you find out the model has stopped honoring the budgets.
  - **Say it once.** A fact belongs in the highest-level place that fits: if a big-picture
    bullet covers a change, that change's `why` is `""`. Repetition across sections was the
    biggest single length driver after entry count.
  - **No entry without analysis.** The prompt says omitting a change marks it routine, but a
    rule floor could still drag it onto the card as a must-read titled `post.py additions`
    with nothing to say. Such an item (`NodeAnnotation.omitted_by_model`, set by analyze's
    default-fill, carried to `Item.no_analysis`) is never must-read: the floor still keeps
    it ON the card — nothing is ever hidden — one line down in the skim list, where the line
    shows the plain-words rule that flagged it ("touches a sensitive area (auth)") instead of
    a bare machine title. Written-up items sort ahead of flagged-only ones, so the capped
    skim slots go to real findings first. The `--no-llm` path sets no such flag: no model
    ran, nothing was omitted, and the floors carry the whole card as before.
  - **No no-op decoration.** The `🔧 code` badge sat on nearly every line and told a reviewer
    nothing; a plain code change now carries no tag and the title leads. The three badges
    that say something the title does not (`🎯 design decision`, `↳ ripple effect`,
    `⚙️ mechanical`) stay. The `**Before:** … **Now:** …` labels are printed by the card, so
    a value opening with its own label ("Before, X did Y" → "Before: Before, X did Y") has
    the lead stripped in coercion (`analyze._strip_label_lead`). Likewise the no-LLM
    breakdown fallback names two chunks inside one function apart ("another part of `f`")
    instead of emitting the same label twice, which read as a duplicated line.
  Same rules for the PR description (D19): its `path:line` pointers are links there too, and
  its footer is ONE small-print line — the keep-hint already says Crux wrote the body, so the
  separate "Description written by Crux…" line above it was pure repetition.
- **D33 — The change map is a user-level flow, written by the review (supersedes D5's
  diagram and D20's node cap):** the shipped map was the code graph with prettier labels —
  one box per changed file/symbol cluster, wired by def/use edges. That is a picture of the
  codebase, useful to a machine that has to walk the diff and useless to the person deciding
  whether to approve it; and above 10 boxes the card silently drew nothing at all, so the
  biggest PRs — the ones most needing a picture — got none. Now:
  - The LLM writes `change_map` alongside `summary`/`overview`: **3-6 steps** (hard cap 8,
    `analyze._MAP_STEPS_MAX`, extras and their arrows dropped) naming what HAPPENS, in ≤5
    plain words each, at the level a person using the software would describe it
    ("Developer pushes a branch" → "Trivial changes skipped" → "Review posted on the PR" →
    "Team told in Slack"). Arrows read "then"; an arrow carries a ≤4-word label only when it
    says something the two boxes do not ("when trivial", "on failure").
  - A step is **never** a file, class, function, module, or config key. Enforced, not just
    asked for: `analyze._is_code_label` judges what is left of a label once code-shaped
    tokens (`post.py`, `_pr_body`, `render_card()`, `WriteBuffer`) are removed, and a D29
    gate drops the WHOLE map when any step fails — cutting one box would break the flow the
    rest of it draws, and a diagram of internals is the thing this decision removed. Labels
    also join the D15 jargon retry and the D32 word-budget lint.
  - The map is drawn whenever it has ≥2 steps and ≥1 arrow, whatever the PR's size — a
    500-file PR still gets its 5-box story. No map is fine and expected: the prompt says to
    return none for a config tweak, a rename, or docs, and the card simply shows no diagram
    (as it does on `--no-llm`, where no model ran). Loose boxes with no arrow are a bullet
    list, not a picture, and are not drawn.
  - `render_card` no longer takes `nodes`/`edges`: the DAG keeps numbering items and setting
    reading order (D5's shared numbering still governs the review lists), but it no longer
    reaches the reviewer as a diagram. Model-chosen step ids are renumbered `n1..nN` at
    render time, so nothing the model invents reaches Mermaid.

### Amendments (2026-07-31)

- **D34 — The PR's own base is the base for the review (tightens D17):** D17 guessed the
  diff base from local git state alone and never asked GitHub. That guess is right for a
  branch off `main` and wrong in exactly the cases stacked work produces, where the card
  then described a diff nobody could see on the PR. Three fixes, at the three points the
  guess broke:
  - **The PR wins.** Once a PR is known (before the description sync, so the commit list is
    right too), `crux run` reads its `base.ref` (`post.pr_base`) and re-bases the whole run
    on it (`gitio.with_base` -> new `base_sha`, `RepoInfo.base_branch`). That covers a PR
    retargeted by hand on GitHub, one opened into a branch Crux never guessed, and one
    GitHub auto-retargeted when its base merged and was deleted. The answer is written back
    to `branch.<name>.cruxBase`, so the correction sticks for the next preview and the next
    run. A base this clone has never seen is fetched once, with a timeout; if it still does
    not resolve, the local guess stands and the run continues. `RepoInfo.crux_base` keeps
    its D17 meaning — where the branch came FROM — and `base_branch` says what the diff is
    AGAINST; the card and the progress comment name the latter. `--base REF` overrides
    everything, including the PR, and is the escape hatch for a preview with no PR at all.
  - **Record the real parent.** The post-checkout hook read `@{-1}`, which names the parent
    only for `git checkout -b child` off the current branch. `git switch -c child parent`
    and `git checkout -b child origin/parent` move HEAD, so the old hook recorded nothing
    at all and the branch silently fell back to `main`. The parent now comes from git's own
    record — the `branch: Created from <start>` reflog entry (`gitio.branch_start_point`) —
    with `@{-1}` kept only for the "Created from HEAD" form, where it is correct.
    `fresh_only` (the entry is the branch's only one) is what tells a creation from a plain
    switch, which git hands the hook identically. `repo_info` consults the same reflog when
    no `cruxBase` is recorded, so branches made before the hook existed get the right base
    too.
  - **A network blip is not a deleted branch.** `_resolve_base`'s existence probe treated
    ANY `gh` failure — expired auth, a 5xx, a timeout — as "the base is gone" and retargeted
    the new PR to `main`. Only a definite 404 counts now (`post._remote_branch_missing`);
    anything else keeps the intended base and logs why.
- **D35 — One branch can carry several PRs, so branch-keyed state must say which one it
  belongs to:** GitHub allows any number of open PRs from one head branch as long as their
  bases differ, which is exactly what D34's stacked work produces (`child -> parent` while
  parent is in flight, then `child -> main` once it lands). Everything keyed by branch alone
  had to answer "which PR?":
  - **The choice is deterministic.** `find_pr` took whichever row `gh` listed first, so the
    card could hop between sibling PRs from push to push, each run fighting the last.
    `post._choose_pr` prefers the PR whose base is the branch the review is already against,
    then the recorded parent, then the default branch — so the diff Crux computes and the
    diff GitHub shows agree without a re-base — and falls back to the OLDEST PR, which stays
    put as new ones are opened. Ambiguity is never silent: the log names every candidate and
    its base, and `--pr N` settles it outright.
  - **The cache is scoped to its PR.** `RunState` lives at `<branch>.json` and carries two
    things that are per-PR, not per-branch: the Slack thread ts and the reused annotations.
    Serving PR #13 the state saved for #12 threaded #13's "new commits" update under #12's
    announcement. `cache.load(info, pr)` now rejects a state whose `pr_number` is some other
    PR (a preview passes None and still gets its reuse; states written before the number was
    recorded still load).
  - **The pre-push answer expires with the work it was given for.** The D11 intent file is
    also branch-keyed, and `ensure_pr` returned early on an existing PR without consuming
    it — so a "yes, open it into X" could sit on disk and fire against an unrelated push
    months later. It is dropped as soon as a PR exists, and honored only while the recorded
    head is still an ancestor of HEAD: new commits made during the 15s delay keep it, a
    reset or force-push elsewhere discards it. Unreadable git discards rather than trusts.
- **D36 — A review retires what it disproves, so memory is not append-only (completes
  D31):** D31 gave reviews one direction only. They could add a fact; nothing but a
  vanished anchor file could take one away. A fact that went stale while its file lived on —
  the convention replaced, the quirk fixed, the pitfall designed out — stayed in every
  future prompt, where a wrong fact is worse than no fact, and the only cure was a human
  noticing and running `crux memory forget`. That made the CLI the store's maintenance
  surface, which it should never have been: the point of D31 is that Crux keeps its own
  knowledge current. Reviews now answer with a `forget` list beside `memories` — the `[id]`
  the prompt already showed them, plus the change that disproves it — and both land in the
  same `memory.absorb` pass, so a fact can be superseded (old id out, new wording in) in one
  atomic write. Three limits keep it a nudge rather than a rewrite: at most 3 retirements per
  review (`_FORGET_MAX`, mirroring the additions cap), the model must cite what contradicts
  the fact rather than merely dislike it, and **a review can never retire a fact a human
  added** — `crux memory add` is how you pin something Crux must not unlearn, and only
  `crux memory forget` drops it. Everything else is unchanged: retirement rides the same
  post-only-after-the-card path (previews never mutate the store), obeys `[memory] enabled`,
  is logged fact-by-fact with its reason, and never reaches the card. Ids the store does not
  have are logged and ignored. `crux memory` is now purely optional — inspection, pinning,
  and the emergency clear.
- **D37 — Super PRs: a cross-repo feature is ONE change, briefed once and merged
  once:** a feature that spans web, api and worker arrives as N
  separate PRs, and reviewing them one at a time hides the only things that matter at
  that scale — what they add up to, and what breaks in the gaps between them. A super
  PR bundles them.
  - **Cross-repo, so Crux owns the bundle.** GitHub's stacked PRs (public preview,
    2026-07-30) come closest, but the API is `/repos/{owner}/{repo}/stacks` —
    single-repo by construction — so a bundle spanning three repos cannot be one.
    `crux/bundle.py` holds the state instead, and with it the rule that makes bundles
    trustworthy: **a PR belongs to at most one super PR**. Without it two bundles could
    each claim a PR and each compute a landing plan the other invalidates. Stacks
    remain useful *within* one repo and are left as a future opt-in, not the substrate.
  - **ONE analysis pass, not N+1.** The combined diff is built by merging the member PR
    heads with `git merge-tree --write-tree`, which "does not read from or write to
    either the working tree or index" — nothing is checked out, no branch is created,
    nothing is pushed, and the user may be mid-edit in any of those clones. That form
    of merge-tree is git 2.38+, and Ubuntu / Pop!_OS 22.04 ship 2.34, where every repo
    failed with git's own usage text and nothing named the fix. So the version is
    checked once, before any repo is touched (`superdiff.require_git`), and an older
    git is one message naming the floor and the upgrade (the git-core PPA there);
    `install.py` warns about it at setup. An unreadable version is "unknown", never
    "too old" — the merge then speaks for itself, and its usage text is translated. Harvest then
    runs per repo (free, deterministic, repo-local signals) and **exactly one** LLM call
    covers the whole bundle (`crux/superanalyze.py`). Running the normal review per PR
    and summarizing the summaries costs an order of magnitude more and still cannot see
    a contract that moved in one repo and its caller in another. Evidence is capped per
    repo (`_NODES_PER_REPO`), which is what keeps a 30-PR bundle inside one call.
  - **Conflicts are findings, not failures.** A PR that will not combine is reported
    with the files it collides on and left out of the combined diff, so the rest of the
    bundle is still analyzed. The two kinds are never conflated: conflicting with your
    own base means *rebase yours* (you would fail to merge alone too), conflicting with
    a sibling PR means *two in-flight changes cannot both land as written* — the finding
    no per-PR review can produce.
  - **One screen, however big the bundle.** The brief is the same length for 3 PRs and
    30; more PRs mean more selective, never longer. Caps live in `crux/superrender.py`
    (`super_ideas_max` 5, `super_checks_max` 7), enforced in render rather than merely
    requested in the prompt, so a model that ignores its budget produces a worse brief
    and never a longer one. Conflicts are exempt from the checks cap — a change that
    cannot land is not an optional read.
  - **The selection IS the bundle.** There is no configured set of repos to declare
    first. The picker offers what `[scope]` already allows — the `repos` allowlist when
    set, else every repo of `owners` — and the PRs picked off that list are the super
    PR. A second place to name repos would be a list to keep in step with the first,
    for no information the user has not already given: a person bundling four PRs knows
    which four, and having to predeclare the group they live in is a form to fill in
    before the useful step. Bundles take a free-text `name` (default: the first
    selected branch), which labels the listing and the issue and binds nothing.
  - **A dry run is one.** `--dry-run` on `new`/`add`/`remove` briefs the bundle the
    change WOULD produce — built in memory, briefed with `publish=False` — and nothing
    else: no bundle file (one left behind listed a super PR nobody made, barred its PRs
    from every other bundle, and was re-briefed for real by the next push to one of its
    branches), no push and no PR opened, since those are the most outward-facing things
    Crux does. A picked branch with no PR has no head to diff yet, so it is named and
    left out of the preview. Likewise a picker or merge confirmation nobody can
    answer — stdin closed or piped — is "no", never a crash (it was an `EOFError`
    reported as "`super` crashed"): nothing picked or merged, and the message says how
    to pass the answer on the command line instead. The open-PRs ask already skips
    itself without a terminal (the picked numbers were the decision); a terminal that
    hits end-of-input there declines, because opening PRs is outward-facing.
  - **The super flow suppresses the single-repo one, and asks its own questions.**
    Selecting a branch with no PR pushes it, and that push would otherwise fire the
    pre-push hook in each of those clones — so bundling five local branches produced
    five D11 "Create a PR for `feat`?" prompts, five commit-enrichment checks, five
    review cards and five Slack messages, which is exactly the N+1 the feature exists
    to abolish. Every push `crux/superpr.py` makes therefore carries `CRUX_SUPER=1`
    (`SUPER_ENV`), and the pre-push body stands down when it sees it. An env var rather
    than `push --no-verify`, so the *repo's own* pre-push hook still runs: Crux
    suppresses Crux, not the user's tests. The prompts are separate for the same
    reason: the D11 ask names a branch and not the repo it is in, which is unreadable
    when a cross-repo feature shares one branch name across four clones. Super asks
    once, before anything is pushed, listing `owner/repo` and branch for every PR it is
    about to open, and prints each PR as it opens with its repo and base.
  - **Membership can change after the fact, and the number never does.** A cross-repo
    change does not always arrive knowing its own extent: the repo nobody expected to
    touch turns up on day three. Before `add`/`remove` the only ways in were to
    hand-edit the stored bundle or to throw the super PR away and rebuild it — which
    costs its number, its brief, and the Slack thread every refresh has been replying
    to. The number is what people link to, so keeping it IS the feature. `add` picks
    from the same candidate list `new` does, which already hides every PR an open
    bundle holds, so what it offers is exactly what can be added; both build members
    through one function so a PR that joined late is not a second-class member.
    `remove` picks from the bundle's OWN members — a separate list, because one list
    showing both would make the numbers mean two things at once — and is membership
    only: the pull request is never closed, commented on or retargeted, and it becomes
    selectable again by construction, since `bundled_prs` reads the bundles. Two things
    it refuses: a member that already MERGED stays, because the brief is the record of
    what landed as one change and dropping a piece of it afterwards would leave the
    record describing something that never happened; and the last member cannot be
    removed, because a brief about nothing is not a reading — `close` retires one.
    Membership is saved BEFORE the re-brief, so a failed refresh leaves the change on
    disk and `crux super refresh` finishes the job rather than starting it.
  - **It lives in an issue.** GitHub has no repo-less issue and no cross-repo grouping
    object, so the brief is filed in the nominated `[super] home` repo and each member
    PR gets a ONE-LINE pointer back to it (`SUPER_LINK_MARKER`) — never a copy, so the
    brief can only ever be stale one way. Cross-repo `owner/repo#12` references link
    automatically and drop backlink events in each member PR's timeline.
  - **Land what can, report the rest.** A cross-repo merge cannot be atomic: N repos
    means N independent merges with no transaction spanning them. Pretending otherwise
    would be worse — a "rollback" that force-pushes reverts across three repos is far
    more dangerous than a half-landed bundle plainly reported. So every PR is attempted
    in the computed order, nothing stops at the first failure, and each blocked PR
    carries an instruction ("required check `build` is failing"), not a status. Re-running
    is safe: merged PRs are skipped.
  - **Slack, threaded on the bundle (D16 extended).** When `[slack] channel` is set, a
    published bundle is announced as its linked GitHub issue number, who wrote it, and
    the repos it touches by bare name — GitHub's number rather than Crux's local
    counter, because that is the one that matches the issue a reader lands in and means
    the same thing on someone else's machine; bare repo names rather than a count,
    because a reader recognises the repos they own. Every later refresh and
    the merge report reply in THAT thread (`Bundle.slack_ts`), so one bundle is one
    conversation rather than a fresh channel post per re-brief. The brief's link is not
    unfurled: it may live in a private repo and an unfurl would render its contents into
    the channel. Best-effort like the per-PR announce — a failure warns on the terminal
    and never blocks publishing.
  - **Candidates come from disk and from GitHub.** The picker lists branches in local
    clones of the in-scope repos (found by origin remote, so a directory renamed locally
    is still placed) plus their open PRs, newest commit first, numbered, with anything
    already bundled filtered out. A selected branch with no PR gets one opened — the
    goal is one command, not a detour to go make PRs by hand. The super flow asks for
    the base branch itself, showing `owner/repo branch → base` for every PR it is about
    to open, because the single-repo D11 ask names a branch and never its repo.
  - **A push to a member branch re-briefs the bundle.** The bundle, not the PR, is what
    a reader of this work looks at, and it goes stale the moment a member moves. So the
    pre-push hook (and the Claude auto-run hook) look the branch up in the local bundle
    store — no network call to decide whether there is work — and detach
    `crux super refresh` instead of the per-PR `crux run`. One change, one card, one
    Slack thread, still.
  - **The brief carries hand-verification steps.** The same `integration_test` the
    per-PR card offers, asked at the bundle's level and held to the same few steps: the
    walkthrough that crosses repos is the one no member PR can give. It is a SECOND
    sticky comment on the brief issue, not part of the brief, for the same reason the
    per-PR card keeps it separate — "what must I look at" and "how do I see it work"
    are different reads, and folding them together is what pushes a brief off its screen.
- **D38 — The brief's buttons: Merge and Close, from the issue, as the person who
  clicked:** the brief is where a super PR is read, and the two things a reader wants
  next ("land it", "abandon it") were a terminal command away in a directory they may
  not even have.
  - **Loopback, because an approval has to carry a name.** The links point at
    `http://127.0.0.1:<port>`, and the SAME markdown works for everyone: loopback
    resolves on the machine of whoever clicked, so the request lands on a Crux already
    authenticated as that person and `gh` approves in their name. A hosted service would
    have to hold everyone's GitHub token; a CI token approves as a bot. `crux serve`
    binds to 127.0.0.1 only — anything already running as that user could run `gh`
    itself, so the port grants nothing new locally and nothing at all remotely.
  - **The brief carries the bundle.** Bundles are MADE on one machine and READ on
    everyone else's, and the only person guaranteed to hold the local `bundles/<n>.json`
    is the author — the one person forbidden to merge. Local-only state therefore made
    every button work for exactly the wrong person ("this machine has no super PR #1").
    So the published brief ends with a hidden `<!-- crux:super-state {…} -->` block, and
    `bundle.hydrate` rebuilds the bundle from it for anyone, saving it locally so the
    lookup is paid once. Three details that are not incidental: the block is appended
    AFTER the length cap, so truncating a huge brief never costs a teammate the bundle;
    `>` is escaped in the JSON, so a branch named `spike/a-->b` cannot close the comment
    early and swallow the rest of the brief; and `home` comes from where the issue was
    found, never from the payload, so a doctored block cannot redirect Crux's next write.
  - **The brief is consulted every time, not cached once.** `hydrate` was local-first,
    and the local copy was written by the reader's OWN first press — so a teammate who
    pressed a button when the bundle held seven PRs saw seven forever, pinned to
    whatever was published the first time they touched it, while the author saw eight.
    "Pays the lookup once" was the bug, not the feature. So the brief is read whenever
    it can be, and the local copy wins only when it is AHEAD: between a `--no-brief`
    edit and the next refresh this machine holds membership the brief has not been told
    about, and a hydrate that reverted its own change would be a trap. What
    distinguishes them is `Bundle.rev`, bumped by every `save` and carried in the state
    block — deliberately a counter and not a timestamp, because the two copies are on
    different machines, `save` restamps `updated_at` on write (so a cache write looks
    newer than the brief it came from), and clock skew between two laptops is not
    something correctness should rest on. A counter only ever has to be compared, never
    subtracted. The cache write is the one write that does NOT count as a revision
    (`_mirror`): a cache that out-numbered the brief it came from would pin itself in
    place forever, which is the original bug one layer down. Ties go to the brief —
    equal revisions mean equal content, except in the one window that cannot be
    numbered (a bundle saved before revisions existed, both sides reading 0), and there
    the published brief beats a cache of unknown age. Taking the brief MERGES rather
    than replaces: the brief is authoritative about what is IN the bundle and carries
    nothing else by design, so `slack_ts`, `created_at` and `closed` are kept from the
    local copy — replacing wholesale would drop the Slack thread and turn one bundle's
    conversation into a fresh channel post per re-brief. An unreachable brief, an
    unauthenticated `gh` or a read-only config dir all fall back to what is here, so
    the buttons still work offline on the last thing this machine saw.
  - **Autostart, from the hooks Crux already has.** The buttons are printed into every
    card and brief the moment `[serve] port` is set, so a service nobody remembered to
    start is a page of dead links — and the failure lands on the reader, who did nothing
    wrong. `serve.ensure_running` therefore runs wherever Crux has just published
    something with buttons in it (`crux run`, `super refresh`), on every push
    (pre-push hook, Claude auto-run hook), and at every Claude Code session start
    (`_crux-hook claude-session-start`, from the plugin and `install-hooks`) — the
    one that brings it back after a reboot, which would otherwise leave dead links
    until the next push: one connect to localhost, and a detached
    `crux serve --port N` when nothing is there. Deliberately NOT a system service —
    Crux's hooks are OS-agnostic sh shims that hand off to Python precisely so nothing
    depends on systemd, launchd or Task Scheduler, and an autostart that worked on two
    platforms of three would be a worse promise than none; this reuses the same
    `_spawn_detached` (setsid / DETACHED_PROCESS) the background review already uses.
    The port is passed on the command line because a detached child resolves config from
    ITS cwd, and a service listening somewhere other than the port printed into the
    cards is the same dead link. `probe` distinguishes "crux" from "something else" from
    "free": a foreign listener is left alone with one log line, never fought for.
  - **`--restart`, because this is the one process that outlives its command.** Every
    other subcommand runs the code on disk right now; the service keeps whatever was on
    disk when it started, so an edit to `crux/serve.py` is live everywhere except the
    one place it matters — and invisibly so, because the stale service still answers.
    `crux serve --restart` stops it and leaves a fresh detached one behind (`--stop`
    just ends it). It stops by pid, which `/health` now returns alongside the service
    name: the port says who it is, and only that pid is signalled. Killing by name is
    the obvious shortcut and the wrong one — `pkill -f "crux serve"` also takes out the
    editor open on the file, and there is no portable process list to match on anyway.
    SIGTERM first, then SIGKILL where it exists: a wedged process still holding the port
    is exactly what a restart is for, so it gets cleared rather than reported.
  - **GET renders, POST acts.** Links inside issue bodies are fetched by things that are
    not people: Slack unfurls, browser prefetch, corporate URL scanners. A GET that
    merged would eventually fire itself with nobody present. So GET returns a page
    naming exactly what will happen, and only a form POST from it — carrying a token
    minted by that process, and refused when `Sec-Fetch-Site` says cross-site — does
    anything.
  - **You cannot approve your own work, at two levels.** GitHub already refuses your
    approval on your own PR; the honest reading is not "merge it unapproved" but "this
    is not yours to wave through". *Wrote the whole bundle* ⇒ no button: raising before
    a single approval is sent beats producing N failures that spell out what one
    sentence says better, and nothing could have landed anyway. *Wrote one of them* ⇒
    the rest is other people's work and you are a real reviewer of it, so it approves
    and lands; yours is skipped, named LOUDLY in the report and in Slack, and the run
    carries on — the same land-what-can policy every other blocker gets. The check
    cannot live in the card: GitHub renders one issue body for everyone, so a link
    cannot be hidden from its author. Everybody sees the button; it works for someone
    else. The terminal (`crux super merge`) and the per-PR card's own Merge button
    enforce the same rule with the same sentence.
  - **Admin merge is the escape hatch, and it is on the record.** Some bundles cannot
    clear the approval path: a repo needing a review nobody available can give, or an
    admin who has read the work and does not want a review record per repo to say so.
    `admin_merge` merges directly on admin rights, approving nothing — so it is the one
    path that can land a bundle its presser wrote. What keeps that honest is the record
    rather than a gate: it requires admin on EVERY member repo (checked, and it names
    the ones you lack), and the merge report and Slack reply both name who overrode and
    state plainly that no approval was recorded. An override nobody can see is the thing
    to avoid; one written where the work is read is a normal, reviewable decision. The
    ordinary per-PR card carries the same pair of buttons (`/pr/<owner>/<repo>/<n>/…`).
  - **Set up to test, because the steps assume all of it at once.** The brief's
    verification walkthrough only works with every member repo on its PR branch, and
    doing that by hand is N fetches and N checkouts in N directories. The third button
    does it and then shows the steps on the same page, so the person who pressed it
    does not go back to the issue to find what they were setting up for (the steps are
    kept on the bundle for exactly this). It fetches, switches and fast-forwards —
    never merges, rebases or stashes. A repo with uncommitted work on another branch is
    reported and left untouched: switching branches under someone's unsaved edits to
    save them a command is not a trade Crux gets to make, and one repo failing must not
    leave the rest half-configured, so every repo is reported with its own verdict.
    "Uncommitted" means TRACKED changes only (`status --porcelain -uno`): untracked
    files are the normal state of a working clone — a build directory, a scratch file, a
    venv nobody gitignored — and counting them refused checkouts on repos with nothing
    at stake, which is how a bundle came back skipped over an untracked folder. The one
    untracked file that DOES matter is the one a switch would overwrite, and git refuses
    that itself; the refusal is reported as a failed SWITCH, separately from a failed
    fast-forward, so the message never names a branch the repo never reached. A
    member repo the presser does not have is CLONED first — a bundle usually spans repos
    somebody has never touched, and cloning is additive and reversible, so it needs no
    ask. It lands in `[super] roots` verbatim, as `<root>/<repo>`; with no roots set,
    the PARENT of the repo the command ran in. Never the current directory: the buttons'
    service starts wherever the user happened to be, usually inside a repo, and cloning
    there would bury a bundle's other repos inside one of its own members. Where clones
    are LOOKED FOR and where a missing one is PUT are therefore one answer from one
    function (`clones.search_roots`, which `clone_root` calls): when they drifted apart,
    discovery searched inside the service's own repo — `_walk` stops at the first `.git`,
    so it found exactly one — while cloning targeted the parent where the clones really
    were, and a reader with all seven repos checked out got `0 of 7 repos ready`, each
    one reported as missing and then as in the way. The second half of that message was
    an assertion nobody checked, so `_clone_missing` now asks the directory what it is:
    a clone of the repo being asked for is USED, and only a clone of something else, or
    no clone at all, is reported — and reported as what it actually is.
  - **Close offers two buttons, never one with a checkbox.** Closing the brief retires
    the reading; closing the member PRs discards open work in several repos, possibly
    other people's. Separate presses, the destructive one plainly marked. A closed
    bundle drops out of `crux super list` and releases its PRs back to the picker, but
    keeps its number — old links keep meaning what they meant.
- **D39 — A renamed branch has no head on the remote, so Crux offers to push it:**
  `git branch -m` renames the local branch and leaves `branch.<name>.merge` pointing at
  the name it had. The next `git push` therefore moves the OLD remote branch and the new
  name never arrives, so `gh pr create --head <new-name>` dies with "Head ref must be a
  branch" / "No commits between …" and takes the whole run down with it. The user's
  work IS pushed; only the name nobody thinks about is wrong, which is why the error
  reads as a Crux failure rather than as the missing push it is. Crux pushes the branch
  instead — `git push -u <remote> <branch>`, where the `-u` matters as much as the push:
  it repoints the upstream at the branch's own name, so the next push needs no rescue.
  - **Asked, never assumed.** A push is outward-facing and not Crux's to make silently,
    so `[pr] push_head` defaults to `ask` (`always` / `never` opt out in either
    direction). The ask happens where a terminal exists: the pre-push foreground
    (`crux ensure-pr`, alongside the D11 base question) records the answer into the same
    intent file, and the detached post-push run acts on it. That run never prompts —
    it has no terminal of its own, so with nothing recorded it leaves the branch alone
    rather than block on `/dev/tty`. A recorded refusal is stored as an explicit `false`,
    not as an absence, so nothing downstream re-decides it.
  - **Asked at pre-push even under `auto_create`.** `auto_create` answers "should there
    be a PR?", not "may I push". A head branch the push will not create is more of a
    problem when a PR is definitely wanted, not less, so that path asks the head
    question too.
  - **Two conditions, because they are decidable in different places.** At pre-push time
    the network says nothing useful — a first push legitimately has no remote branch yet
    — so the ask is gated on the rename signature alone: the branch tracks a remote
    branch under a DIFFERENT name (read from `branch.<name>.remote`/`.merge`, not from
    parsing `@{u}`, since a remote name may itself contain a slash). At create time the
    gate is a definite 404 on the head (D34 again): a 502 or an expired token must never
    push a branch nobody asked about.
  - **A failed push is not rewritten.** `push_head` logs and returns False, leaving
    `gh pr create` to fail with its own accurate error — an invented one would bury it.

- **D40 — Zenhub tickets close when the work that closes them lands:** a ticket and the
  pull request that implements it are two records of one piece of work, and keeping them
  in step is bookkeeping nobody does reliably. Crux already knows when a PR lands — it is
  the thing that lands it — so it is the natural place to retire the ticket. Cross-repo
  is where this earns its keep: GitHub's own `Closes #N` only auto-closes within one
  repo, so exactly the case super PRs exist for is the case GitHub cannot serve.
  - **Optional, and off by default.** `[zenhub] workspace` empty (the default) means
    `zenhub.enabled()` is False and every entry point returns immediately: no calls, no
    prompts, no behaviour change anywhere. Both halves are required — a workspace with
    no key cannot be reached, a key with no workspace has nothing to point at — and
    `why_disabled()` names whichever is missing. The automatic paths make no Zenhub
    calls when it is off, with one exception: when Crux opens a PR, it prints a single
    note naming what is missing, so a machine that was never set up does not look like
    one where the feature is broken. It is a note, not a warning, because Zenhub is
    optional; it appears once per PR, not on every push, and `[zenhub] ask = false`
    silences it.
  - **Two shapes, one store.** A regular PR is one PR in its own repo; a super PR is
    several PRs plus a brief issue in the `[super] home` repo. The brief is Crux's own
    artifact, not a ticket, so it is never the thing closed. Tickets therefore hang off
    the PR in the first case and off the BUNDLE in the second, keyed
    `pr:owner/repo#12` and `super:7` in one index. A bundle's tickets close only once
    EVERY member has merged: a cross-repo ticket is not done while half its repos are
    unmerged. `crux zenhub link` on a branch inside an open bundle refuses and points at
    `--super N`, the same guard `crux merge` already carries.
  - **Inferred, shown, confirmed — never guessed silently.** Crux ranks the workspace's
    open tickets against the branch name, PR title and commit subjects (an explicit
    `issue-29` outranks everything; shared words order the rest) and shows the top
    handful with each ticket's **number, title and description**. Enter links nothing.
    Generous inference is safe precisely because a human confirms: a wrong candidate
    costs one line in a list already being read. `[zenhub] ask = false` drops the offer
    without dropping the feature, and `crux zenhub link` covers any PR at any time —
    including PRs Crux never opened.
  - **Asked at pre-push, because that is the only place a terminal exists.** The review
    a push triggers runs DETACHED, and a detached process cannot open `/dev/tty` by name
    — so a picker offered there would silently answer itself "none" on every push, which
    is the whole flow. The ask therefore joins the D11 base question and the D39 head
    question in the pre-push foreground, last of the three because it is the least
    urgent and the only one a single later command can still answer. The choice is
    RECORDED and applied by the detached run once the PR exists — on a first push it
    does not yet, since that run is what creates it. Held in its own file rather than
    the D11/D39 intent file: `post.consume_intent` is emptied the moment `ensure_pr`
    runs, including when a PR already exists, where the base answer is moot but a ticket
    choice is not — sharing it would throw the answer away in the commonest case there
    is. Same HEAD stamp and ancestor guard, so a choice stranded by a run that never
    happened cannot be applied, branch-state later, to work nobody answered for.
  - **The terminal is probed before the network.** The gates run cheapest-first — Zenhub
    off, ask off, already linked, a choice already waiting, no tty — so an ordinary push
    never pays a Zenhub round trip for a question that was never going to be asked, and
    a hook with no terminal (CI, an IDE, an agent) pays nothing at all.
  - **One closing authority.** The close hangs off `crux/superact.py`, which every merge
    goes through, because there are two doors onto every merge — the terminal and the
    brief's Merge button — and one policy has to serve both. The CLI only reports what
    was closed. Closing sets the ticket's state (and with it the GitHub issue behind
    it); the pipeline move is a separate call because it is a separate statement, and
    boards do not always move a card on close by themselves.
  - **A command, not a daemon.** Crux has no webhook and is not growing one, so a PR
    merged from the GitHub UI is caught by `crux zenhub sync` — which anyone can run, or
    cron. That is honest about what it is, where a background poller pretending to be
    live would not be. `None` from a PR-state read is never treated as `True`: a network
    blip must not close a live ticket. Every close is idempotent, so syncing twice, or
    syncing right after a merge, costs nothing.
  - **Best-effort, always.** Every Zenhub call swallows its errors and returns None, like
    Slack: a ticket left open is a nuisance, a merge that refuses to run is not. The
    retire step in particular runs AFTER the merge has already happened, so it swallows
    broadly on purpose — raising there would report a successful landing as a failure.
    The link Crux relies on lives in its own store, so a failed Zenhub connection or a
    failed PR comment loses a signpost, never the link.
  - **The schema is one block.** Zenhub's public GraphQL API documents some of what Crux
    needs thinly and `createIssuePrConnection` barely at all, so every query lives in one
    block at the top of `crux/zenhub.py`, a field error is logged as schema drift rather
    than a generic failure, and `crux zenhub doctor` introspects the live schema and says
    which calls the server actually accepts. The connection mutation is treated as
    optional throughout: without it linking and closing still work, and only the line
    drawn on the Zenhub card is lost.

- **D41 — A human can pin a super PR's landing order and merge method (amends D37/D38):**
  the landing order was the review pass's (`bundle.order = annotation.order`), and
  every re-brief chose it again — including the automatic one a push to a member branch
  triggers — so an order a person had decided (a dependency the diff does not show,
  branches stacked on each other) could not stick: the next push quietly undid it. And
  both doors onto the merge, `crux super merge` and the brief's button, squashed with no
  way to say otherwise, which is wrong for PRs built on each other's commits: once the
  first is squashed, the next conflicts with the very commits it contains.
  `crux super order N [REF …] [--unpin] [--method M]` gives the human the last word on
  both, and with no arguments shows the current order and method.
  - **A whole order or none.** REFs are `owner/repo#N`, or `repo#N` when the repo name
    is unique in the bundle. Every member still to land must be listed, exactly once;
    members that already merged may be left out, and go first — they have landed. A
    prefix is refused: a pin says "a person chose this sequence", and letting the
    unlisted rest trail in whatever order happens to be stored would print "pinned" over
    choices nobody made. The strictness costs one paste: the refusal prints the full
    command, and `crux super order N` alone prints the current order as one to edit.
    Everything is validated before anything changes, so a typo never leaves a bundle
    half-edited.
  - **The pin survives membership changes.** A re-brief keeps it (`_settle_order`). A
    member added later goes LAST — everything already listed was placed by a person, and
    a newcomer has no claim to go ahead of it — and a removed one drops out. `--unpin`
    hands the order back to the review pass at once, from its last proposal, rather
    than leaving a person's order in place under a brief that no longer says it is
    pinned.
  - **The model still speaks, as advice.** A re-brief of a pinned bundle still gets the
    model's order — it is one field of the one call, so asking costs nothing — and keeps
    it on the bundle (`suggested_order`, `order_why`). The brief shows it only when it
    disagrees with the pin ("the review pass suggested B → A: B adds the API A calls"),
    so a dependency the model spotted is still in front of the reader, labelled as a
    suggestion rather than as the plan.
  - **Method: `--method` > the bundle's > `[super] merge_method` > squash.** Resolved in
    exactly one place (`supermerge.resolve_method`, called by `superpr.merge`, which both
    doors reach), so the terminal and the button cannot come to two answers. Squash stays
    the last word, so a bundle and a machine that set nothing merge exactly as before.
    Only the bundle's own method binds a teammate: config is per machine, and the button
    runs on whoever clicked, with THEIR config. So the brief prints a bundle's method in
    bold and a fallback as "(default)", and the confirm page names the method it is about
    to use and where it came from. The single-PR `crux merge` and the per-PR card keep
    their own `--method` and squash: `[super] merge_method` is a super PR setting, and
    one that quietly changed how ordinary PRs merge would be a surprise.
  - **It travels in the brief.** `order_pinned`, `merge_method`, `suggested_order` and
    `order_why` join the D38 state block and `_adopt`, so a teammate's Crux lands in the
    pinned order with the pinned method — the whole point, since the author is the one
    person who cannot press Merge. A brief published before D41 has none of the keys and
    reads as unpinned with no method, which is exactly what it meant. The method is
    checked against the three GitHub accepts on the way in: the block is text anyone who
    can edit the home repo's issues can change, and the value ends up in a merge call.
  - **Restamped, not re-reviewed.** An order or a method is a decision, not new
    evidence, so the brief is rewritten in place (`superrender.restamp`, then the normal
    `superpost.publish`): the "Landing order" section and the state block are
    regenerated from the bundle, everything the model wrote is kept exactly as published,
    and there is no diff, harvest or model call. Re-running the review to print a
    different arrow would cost a model call and rewrite analysis nobody asked to change;
    a `--no-llm` refresh would throw the analysis away. The bundle is saved first, so a
    publish that fails leaves the decision on disk for the next refresh; and a brief that
    cannot be read — an empty read is how `issue_body` fails — is never overwritten.
  - **The brief shows the order the merge follows.** The landing section now renders
    `supermerge.order_members(bundle)` instead of re-deriving an order from the model's
    list. The two disagreed whenever the model left a member out — the brief appended it
    alphabetically, the merge in member order — and a plan that is not the plan is worse
    than none.


## The card (reviewer-facing comment)

The review card (D20) — leads with the big picture, then the user-level change map (D33) and
tight, badged review lists:

```markdown
<!-- crux:card -->
## Crux · `a3f9c12` · ~4 min read
_Batch draft saves so a crash loses at most 5s of edits._

### The big picture
- Draft saves are batched through a new in-memory buffer, cutting DB calls — `editor/autosave.py:1-88`
- The editor and storage layer now talk over that buffer, not directly — `editor/main.py:88`
- ⚠️ A failed flush drops the whole batch instead of retrying — `editor/autosave.py:61`

### Change map
```mermaid
graph LR
  n1["Editor takes a keystroke"]
  n2["Waits in the 5s buffer"]
  n3["Batch written to the database"]
  n1 --> n2
  n2 -->|every 5 seconds| n3
```

### 🔴 Must read
- **Drafts are kept in memory and saved in batches** · [editor/autosave.py:1-88](permalink) · [diff](anchor)
  Nothing protects the buffer from two writers at once, and flush swaps it out mid-write.
- 🎯 design decision · **A crash loses up to 5s of edits** · [config/editor.toml:12](permalink)

### 🟡 Worth a skim
- ↳ ripple effect · **flush-on-shutdown handler** · [editor/main.py:88](permalink)
- **db.py edits** · touches a sensitive area (migrations) · [db/schema.py:20-31](permalink)

### 🟢 Safe to skip
588 lines across 3 groups, machine-checked — no review needed.
```

The how-to-test comment (D22) is a second sticky comment, when warranted:

```markdown
<!-- crux:integration-test -->
## 🧪 How to verify this
**Prerequisites:** make sure the project is installed and set up first — see [Install](…/README.md#install).

1. Start the editor and type 100 edits, then kill it mid-run
2. Confirm at most ~5s of events are missing
```

The PR description itself (D19) starts as the commit list, then is rewritten from the review's
summary + overview (trivial commits curated out) and links both comments:

```markdown
Batch draft saves in memory so slow storage never blocks typing.

- Writes queue in a bounded buffer and flush in batches — [`editor/autosave.py:42`](permalink)
- Shutdown drains the buffer, so a crash loses ~5s — [`editor/autosave.py:118`](permalink)
- 📋 [Crux review](…#issuecomment-…)
- 🧪 [How to test this](…#issuecomment-…)

<sub>🤖 Written by Crux, which keeps this PR's title and description in sync with the branch. Add `<!-- crux:keep -->` to keep your own.</sub>
```

Rules: overview bullets and titles are plain English (D15, no tool jargon), written to the D32
word budgets, each linked to the code. A badge (D4) tags a review line only when it says
something the title does not (D32). Crux never says "looks good" — it ranks, evidences, and
leads with the ideas that matter. A failed run posts a loud one-line failure comment update and a
red status check, never silence. Comment body hard-capped ≤ 60,000 chars (GitHub limit 65,536).

## Pipeline — what `crux run` does, step by step

0. **Look at the repo** (`repo_info`) — figure out which branch we're on, the branch it was
   created from (its `cruxBase`, D17) which is the base for the diff, and who owns the repo on
   GitHub. If the owner isn't in the `[scope] owners` allowlist, stop right here and do nothing
   (D13). Then ensure a PR exists before doing any analysis (D18), and re-base everything
   below on the branch that PR actually merges into — the local guess above is only a guess
   (D34).
1. **Gather the facts** (`harvest`, ~10 seconds, no AI) — read the diff and split it into
   changed chunks. For each chunk: label what kind of change it is (real behavior change /
   the same edit repeated in many places / a generated file / formatting only); count how many
   places in the repo call the changed code; check git history for files that break often and
   for "these two files usually change together, but only one changed" cases; check whether
   the changed lines have tests; and record which chunks create new names (functions, classes)
   and which chunks use those names.
2. **Decide if a report is even worth it** (`gate`) — if the change is small, nothing sensitive
   was touched, and nothing widely-used was modified: stop and post nothing (D6). Just write
   one line to the log saying why.
3. **Order the changes** (`dag`) — group related chunks into numbered items. Draw an arrow
   between two items when one exists only because of the other (it uses a function or class
   the other one created). The item nothing points at is the heart of the PR; following the
   arrows gives the reading order. This ordering is internal: it numbers the review lists and
   feeds the AI step, and is never shown as a diagram (D33).
4. **Ask Claude to explain it** (`annotate` — the only AI step) — write the plain-English
   title, the "why this matters" text, and the check-questions for every item; draw the
   change map the reviewer actually sees: a handful of steps saying what happens, end to end,
   in the words someone using the software would use (D33); list each thing
   the PR description claims and verify it against the actual code; use the AI author's own
   session notes when they exist; recall what earlier reviews learned about this repo and
   propose up to 3 new durable facts worth remembering — and retire up to 3 the PR just
   disproved (D31/D36); and for items unchanged since
   the last push, reuse last run's writing instead of paying to regenerate it (D9).
5. **Sort into red / yellow / green** (`tiers`) — fixed rules set a floor first, and Claude's
   opinion can raise an item but never lower it below its floor.
6. **Write the card** (`render`) — turn everything into the markdown comment, following the
   link, badge, map, and plain-English rules (D3/D4/D33/D15).
7. **Post it** (`post`) — the PR was ensured up front (D18) and its title/description already
   synced from the commits (D19); now update the single Crux review comment on it, add the
   how-to-test comment when warranted (D22), re-sync the description to link both comments and
   set the LLM title, flip the status check to success (D21), fold the review's proposed
   repo facts into the memory store and retire the ones it disproved (D31/D36),
   announce/thread to Slack (D16), and save this
   run's results (incl. the Slack thread ts) so the next push can reuse them (D9).

Note (D18): the PR is actually ensured **before** step 1 — with no PR, Crux stops before any
diff. Steps 1–6 run only once a PR exists. Design/standards findings (D14) are still validated
if the model volunteers them but are no longer requested or shown on the card (D20).

The fixed tier rules from step 5, spelled out — Claude can never override these downward:
- Touches a sensitive area (auth, billing, payments, CI workflow files…) → always **red**.
- Changes behavior AND is called from 5 or more places → always **red**.
- Weakens a test (deleted assertion, skipped test, loosened threshold) → always **red**.
- Otherwise, an item made only of test-file changes is capped at **yellow** (D25) — tests ride
  along with the code they test, and sensitive keywords inside a test can't force red.
- Only repeated-edit or generated chunks that nothing else calls can even qualify for
  **green** — and only after a machine check proves they're harmless (identical edits, a
  regenerated lockfile, and so on).
- Everything else starts **yellow**.

The skip rule from step 2, spelled out — a PR is skipped only when ALL of these are true:
fewer than 50 lines of real behavior change, no sensitive areas touched, no dependency files
changed, and nothing that's called from 5+ places modified. Every number is configurable in
`crux.toml [gate]`.

## Module contracts (build against these; shared types in `crux/models.py`)

| Module | Public API |
|--------|-----------|
| `crux/gitio.py` | `run_git(args, cwd=None, timeout=None) -> str` · `git_version() -> (major, minor, patch) \| None` (None = unknown, never "too old") · `repo_info(cwd=None, base_ref=None) -> RepoInfo` · `with_base(info, base_branch) -> RepoInfo` (D34: re-base on the PR's real base, fetching it once if unknown) · `branch_start_point(branch, cwd, fresh_only=False) -> str \| None` (the `branch: Created from` reflog entry) · `diff_hunks(info) -> list[Hunk]` (parses `git diff -U3 base...head`, fills `enclosing_symbol` by regex scan upward for `def/class/function/fn/func` lines) |
| `crux/harvest/structural.py` | `classify(hunks, cfg) -> None` (sets `Hunk.klass`; generated by path patterns; cosmetic via normalized-line compare; uses `difft` if present) · `mechanical_clusters(hunks) -> list[list[str]]` (groups of hunk ids whose normalized added-lines are identical) |
| `crux/harvest/defuse.py` | `extract_defs_uses(hunks) -> dict[str, HunkSignals]` (defines = symbols introduced/renamed/re-signed in added lines; uses = identifiers in added lines ∩ union of all defines across the diff) |
| `crux/harvest/blast.py` | `add_blast(signals, hunks, repo_root) -> None` (rg `-w` count per defined symbol across repo excluding the defining file's hunk range; fills `blast_radius`, `callers` samples) |
| `crux/harvest/history.py` | `add_history(signals, hunks, repo_root, cfg) -> None` (churn + fix-frequency via `git log --since`; co-change partners ≥70% together over window → `co_change_miss` when partner absent from diff) |
| `crux/harvest/testprox.py` | `add_test_proximity(signals, hunks, repo_root, cfg) -> None` (rg defined symbols under test dirs → `test_touched`) |
| `crux/config.py` | `load(repo_root) -> Config` (layers `$XDG_CONFIG_HOME/crux/crux.toml` then the repo's `crux.toml`, repo winning key by key, via tomllib; all fields defaulted; no files ⇒ pure defaults) |
| `crux/gate.py` | `decide(hunks, signals, cfg) -> GateDecision` |
| `crux/dag.py` | `build(hunks, signals, clusters) -> tuple[list[DagNode], list[DagEdge]]` (node = same file+symbol cluster or mechanical cluster; edge src→dst when dst uses a symbol src defines; numbering = topological order, ties by max signal score; provisional badges: root→CODE_CHANGE, interior/leaf behavioral→CODE_CHANGE_EFFECTS, mechanical cluster→MECHANICAL_CHANGES) |
| `crux/llm.py` | `claude_json(prompt, cfg, timeout=600) -> dict` (subprocess `claude -p --output-format json --model cfg.llm_model`, extract first JSON object from result text, one retry on parse failure, raise `LlmError` after) |
| `crux/analyze.py` | `annotate(nodes, edges, hunks, signals, intent, previous, cfg, info=None, memories=None) -> Annotation` (builds prompt from the packaged `crux/prompts/analyze.md` template; passes previous RunState summary for incremental reuse and the repo's remembered facts (D31); returns claims, per-node annotations keyed by node number, up to 3 proposed memories, and up to 3 memory ids to retire (D36); marks DESIGN_DECISION) |
| `crux/tiers.py` | `assign(nodes, hunks, signals, annotation, cfg) -> list[Item]` (applies floors; computes per-item permalink line ranges; minutes budget) |
| `crux/render.py` | `render_card(info, pr_number, items, annotation, gate_stats) -> str` (the change map comes from `annotation.change_map`, D33 — the DAG is not rendered) · `permalink(info, path, a, b) -> str` · `diff_anchor(info, pr, path, line) -> str` |
| `crux/post.py` | `find_pr(info) -> int \| None` (D35: deterministic when a head has several open PRs) · `pr_base(info, pr) -> str \| None` (D34: the branch the PR merges into) · `ensure_pr(info, cfg, interactive=True) -> int \| None` (D11 prompt via /dev/tty; D39: pushes a missing head branch first when agreed) · `tracked_branch_mismatch(info) -> str \| None` (D39 rename signature, from git config, no network) · `record_push_head_intent(info, cfg) -> bool` (D39 pre-push ask) · `push_head(info, cfg) -> bool` (`git push -u`; never raises) · `consume_intent(info) -> dict` (all recorded pre-push answers, once) · `upsert_comment(info, pr, body) -> None` (marker search, PATCH else POST — via `gh api`, or the same requests through `crux.ghrest` on a gh-less host) |
| `crux/prs.py` | `resolve_repos(repo_args, cfg) -> (repos, problems)` (no args ⇒ every repo of the `[scope]` owners via `gh repo list`; each arg is `owner/name` verbatim, a bare name matched against the owners' repos, or `.` = the current repo's origin; never raises — bad args degrade to problem lines) · `fetch_open_prs(repos, jobs) -> dict[repo, rows \| error-str]` (one `gh pr list --json` per repo on a thread pool of ≤ jobs workers) · both `gh` calls here are argv with no REST equivalent, so this module needs `gh` even on hosts where posting does not (D12) · `render_prs(results) -> str` (aligned per-repo blocks + summary line) · `auto_jobs() -> int` (min(32, cpu+4) — the default "max parallel tasks" pool size when `[prs] jobs` is 0) |
| `crux/cache.py` | `load(info, pr=None) -> RunState \| None` · `save(info, state) -> None` (`~/.cache/crux/<owner>__<repo>/<branch>.json`; `load` returns None if `base_sha` mismatch, or if the state belongs to another PR on this branch, D35) |
| `crux/clones.py` | D37: `qualify(repos, cfg) -> list[str]` (bare names take the first scope owner) · `search_roots(cfg, repo_root) -> list[str]` (unset ⇒ the parent of the current repo, climbing out of the work tree the caller is in; `superact.clone_root` calls this so looking and cloning cannot diverge) · `clone_slug(path) -> str` (which repo a clone points at, detached HEAD included) · `find_clones(roots, wanted, jobs=8) -> dict[slug, LocalClone]` (placed by origin remote, not directory name; newer tip wins on duplicates; empty `wanted` ⇒ every clone found) · `age(ts, now=None) -> str` |
| `crux/candidates.py` | D37: `scope_repos(cfg) -> (list[str], problems)` (`[scope] repos` when set, else every repo of `[scope] owners`) · `gather(cfg, repo_root=None, editing=None) -> (list[Candidate], problems)` (local branches ∪ open PRs, minus anything already bundled, newest commit first; falls back to disk alone when no repo can be listed) · `render(cands, now=None) -> str` (the numbered picker) · `parse_selection(text, count) -> (indices, problems)` (`"1,3-5"`) |
| `crux/bundle.py` | D37: `load(n) -> Bundle \| None` · `load_all() -> list[Bundle]` · `save(bundle)` (atomic) · `delete(n) -> bool` · `next_number() -> int` (monotonic; numbers are never reused) · `key(owner, repo, pr) -> str` · `find_by_branch(owner, repo, branch) -> Bundle | None` (the push-time membership lookup; local state, no network) · `encode_state(bundle)` / `decode_state(body, home)` / `strip_state(body)` / `hydrate(number, cfg, home="")` (D38: the bundle carried inside its own brief, so any machine can rebuild it — D41 adds the pin, the merge method and the model's suggested order to it; reconciles local against published by `rev`, local winning only when strictly ahead, ties to the brief, network failure to local) · `save` bumps `rev`, `_mirror` writes a cache of someone else's brief without bumping · `bundled_prs(exclude=None) -> dict[str, int]` (the one-PR-one-bundle index, minus closed bundles; store: `$XDG_CONFIG_HOME/crux/bundles/<n>.json`) |
| `crux/superdiff.py` | D37: `build(owner, repo, members, local_path=None, default_branch="main") -> RepoDiff` · `build_all(members, paths, defaults) -> (list[RepoDiff], problems)` (merges PR heads via `git merge-tree --write-tree` — never touches the working tree, index, or remote; conflicts become `Conflict` findings and the PR is left out of the combined diff) · `require_git()` (raises once, with the fix, below `MIN_GIT` = 2.38; `build_all` calls it first) |
| `crux/superanalyze.py` | D37: `harvest(diffs, cfg) -> list[RepoEvidence]` (the normal harvest per repo, no LLM) · `build_prompt(bundle, evidence, diffs, cfg, memories) -> str` (from `crux/prompts/super.md`) · `annotate(...) -> SuperAnnotation` — **exactly one LLM call per bundle, whatever its size** |
| `crux/superrender.py` | D37: `render_card(bundle, ann, diffs, cfg) -> str` (one screen; caps enforced here, conflicts exempt) · `render_backlink(bundle, url, member) -> str` · `render_merge_report(bundle, results, note="") -> str` · `render_test_comment(bundle, steps) -> str` · `render_actions(bundle, cfg) -> list[str]` (D38: the loopback Merge/Close links; empty when `[serve] port` is 0) · `render_landing(bundle, cfg, suggested, why) -> list[str]` (D41: the order the merge follows, the merge method, and — when pinned — the model's disagreeing suggestion) · `restamp(body, bundle, cfg) -> str` (D41: a published brief with only its landing section and state block redone) |
| `crux/superact.py` | D38 the brief's buttons, shared by the loopback service and the CLI: `actor_login()` (whoever this Crux's `gh` is) · `authored_by(bundle, login) -> list[BundleMember]` (live from GitHub, never from stored state) · `merge(bundle, cfg, login="", method) -> (results, summary, problems)` (approves every member PR as the caller, then `superpr.merge`; raises `ActError` BEFORE any approval when the caller wrote ALL of it, and skips-with-a-loud-reason the ones they wrote when they wrote some) · `admin_merge(...)` (merges on admin rights, approving nothing; requires admin on every member repo and stamps the override into the report + Slack) · `admin_on(slug) -> bool` · `merge_pr(slug, pr, ...)` / `admin_merge_pr(slug, pr, ...)` (the same two buttons for one ordinary PR) · `refusal(bundle, login, mine) -> str` (one wording for browser, terminal and plugin) · `ask_for_merge(...)` (Slack thread ask) · `checkout(bundle, cfg, repo_root=None) -> list[Checkout]` (every member repo onto its branch; missing ones cloned into `clone_root`, then fetch + switch + ff-only, never a merge/rebase/stash, dirty repos skipped) · `clone_root(cfg, repo_root=None) -> str` (`[super] roots` verbatim, else the repo's parent, never the cwd) · `close(bundle, cfg, with_prs=False) -> problems` |
| `crux/serve.py` | D38 `serve(cfg, port)` — `ThreadingHTTPServer` bound to 127.0.0.1 only. `GET /super/<n>/merge|checkout|close` and `GET /pr/<owner>/<repo>/<n>/merge` render confirm pages (never act), `POST` the same paths act (`admin-merge` too), gated on a per-process token and a `Sec-Fetch-Site` check; `GET /health` for liveness, returning the service's own pid. `ensure_running(cfg, log) -> "crux"|"other"|"free"|"disabled"` (the D38 autostart) · `probe(port)` · `service_pid(port)` · `stop(port) -> "stopped"|"free"|"other"|"failed"` (SIGTERM then SIGKILL, by pid, never by name) · `restart(cfg, port, log) -> "restarted"|"started"|"other"|"disabled"|"failed"` (behind `crux serve --restart`: the service is the one process that outlives its command, so an edit to Crux's own source never reaches it otherwise) · `start_background(cfg, port)` for tests |
| `crux/superpost.py` | D37: `publish(bundle, card) -> (issue_number, url)` (sticky issue in the bundle's home repo) · `link_members(bundle, url) -> problems` · `comment(slug, n, body)` · `close_issue(bundle)` · `publish_test_steps(bundle, steps)` (D37: the sticky verification comment under the brief) |
| `crux/slack.py` (D37 additions) | `super_id(bundle) -> str` (GitHub's issue number, not the local counter) · `repo_names(members, cap=4) -> str` (bare repo names, deduped, `+N` past the cap) · `announce_super(cfg, bundle, url, thesis, previous_ts, authors) -> (channel, ts)` (keyed on the bundle, not a repo; the id links the brief; threads on re-brief; no unfurl) · `announce_super_merge(cfg, bundle, url, results, root_ts) -> (channel, ts)` (replies in the bundle's thread with what landed and what blocked) |
| `crux/supermerge.py` | D37: `run(bundle, method="squash", skip=None) -> list[BundleMember]` (land-what-can; sets `state`/`error` per member) · `order_members(bundle)` (members missing from `order` land last, never dropped) · `summary(results) -> str` · `resolve_method(bundle, cfg, explicit="") -> str` (D41: `--method` > the bundle's > `[super] merge_method` > squash) · `method_label(method) -> str` |
| `crux/superpr.py` | D37 orchestration shared by the CLI and the plugin: `home_repo(cfg) -> str` (`[super] home`; an error, never a guess) · `needs_pr(picks) -> list[Candidate]` (the selected branches with no PR yet — what the front end names before pushing) · `create(cfg, picks, name="", progress=None, base="", dry_run=False) -> (Bundle, problems)` (opens PRs for selected branches without one, pushing with `SUPER_ENV` set so the pre-push hook stands down; unnamed bundles take the first member's branch) · `add(cfg, bundle, picks, ..., dry_run=False) -> (Bundle, problems)` and `remove(bundle, picks, dry_run=False) -> (Bundle, problems)` (membership after the fact, keeping the number; `dry_run` returns an unsaved copy and opens no PR) · `_members_from(...)` (shared by `create` and `add`) · `render_members(bundle)` (numbered, in stored order, for the remove picker) · `refresh(bundle, cfg, ...) -> (card, url, problems)` (D41: keeps a pinned order, appending newcomers) · `merge(bundle, cfg, method="") -> (results, summary)` (resolves the method, D41) · `resolve_refs(bundle, refs) -> list[str]` / `set_landing(bundle, refs=None, unpin=False, method=None) -> Bundle` (D41: a whole order or none, validated before anything changes) · `republish(bundle, cfg) -> (url, problems)` (D41: restamp the brief, no model call) · `render_landing_plan` · `render_list` · `render_show` |
| `crux/memory.py` | D31: `load(info) -> list[Memory]` · `save(info, memories)` · `absorb(info, proposed, cfg, retract=None) -> (store, notes)` (the ONE write path for review facts: dedupe by id, drop/forget dead anchors, retire the ids the review disproved unless a human added them (D36), cap with human facts last) · `add(info, text, anchor) -> Memory \| None` · `forget(info, ids) -> (removed, missing)` · `clear(info) -> int` (store: `$XDG_CONFIG_HOME/crux/memory/<owner>__<repo>.json`) |
| `crux/commitmsg.py` | `enrich(info, cfg, sha) -> str \| None` (D27: one LLM pass over the commit's diff via the packaged `crux/prompts/commit.md`; amends the message in place — human text on top, `Crux: <subject>` + bullets below, `Amended-by: Crux` trailer; guarded twice against HEAD moves / staged work / in-flight rebase / pushed commits; returns the new subject when amended) · `ensure_enriched(info, cfg, refs=None, notify=None) -> bool` (D28: enrich every outgoing human commit via a `commit-tree` chain rebuild + CAS `update-ref`; True ⇒ the push's shas are stale, stop it) · `effective_subject(message) -> str` (Crux subject for amended messages, else line 1 — used by every commit-based PR metadata source) |
| `crux/cli.py` | `main()`; subcommands: `run [--no-llm] [--dry-run] [--delay N] [--yes]`, `preview` (= run --dry-run, print card), `enrich-commit [--sha SHA]` (D27, spawned detached by the post-commit hook), `ensure-enriched [--hook]` (D28, pre-push foreground; exit 1 = stop this push, push again), `prs [REPO ...] [--jobs N]` (open PRs across the current/named/all-scope-owner repos, parallel `gh pr list` per repo; exit 1 when nothing could be listed — the one command that runs only interactively, never from a hook), `install-hooks [--local]` (global by default, once per machine: pre-push + pass-through shims into a `core.hooksPath` dir, Claude Stop hook merged into `~/.claude/settings.json`; `--local` scopes both to the repo: `.git/hooks` + `.claude/settings.json`), `super {new,add,remove,refresh,merge,order,checkout,ask,close,show,list}` (D37 cross-repo bundles; `order` is D41; interactive-only like `prs`/`memory`, so it may exit 1), `merge [--pr N] [--admin] [--method M] [--yes]` (D38: approve this branch's PR as you and merge it, on the same `superact` code as the card's button; a branch inside a super PR is sent to `crux super merge`), `serve [--port N]` (D38 the buttons) |

Errors: every stage raises typed exceptions; `cli.run` catches, and if a PR exists posts the
loud one-line failure update (D8); always exits 0 from hook contexts.

## Hooks

**OS-agnostic design.** The installed git hooks are thin POSIX-`sh` shims (`cli._hook_shim`)
that git runs through its own bundled shell on every platform (Git for Windows included). Each
shim delegates to any repo-local hook of the same name, then `exec _crux-hook <name>` — a
**private console script** (entry point `crux.cli:hook_main`), deliberately not a `crux`
subcommand, so the machinery stays out of the user-facing surface. All hook logic lives in
Python (`cli._cmd_hook_*`), so nothing depends on bash, `nohup`/`disown`, or a POSIX-only tty.
Two small pieces are the only OS-specific code, each isolated to one helper: portable process
detachment (`cli._spawn_detached`: `start_new_session` on POSIX, `DETACHED_PROCESS |
CREATE_NEW_PROCESS_GROUP` on Windows) and terminal I/O (`post._tty_devices`: `/dev/tty` on
POSIX, `CONOUT$`/`CONIN$` on Windows).

- **pre-push** (`_crux-hook pre-push`) — foreground: scope check, then `ensure-enriched` (D28,
  stdin ref lines passed through; return 1 ⇒ messages were enriched ⇒ the shim propagates it to
  stop THIS push with a "push again" notice); then, if no PR exists, the D11 ask (the prompt
  reaches the terminal via the OS tty device, opened as two one-way handles to avoid a
  non-seekable `r+` crash); then spawn `crux run --delay 15 --yes` **detached** (via
  `_spawn_detached`) so analysis happens after the push lands. Terminal progress lines come from
  the detached run, not here (D21). Never blocks otherwise; foreground time <2s plus the
  optional prompt, except when D28 has messages to enrich (one LLM call per terse commit, then
  the stop-and-retry).
- **post-checkout** (`_crux-hook post-checkout`) — records `branch.<name>.cruxBase` for a newly
  created branch (D17), silently, from the start point git wrote to the branch's reflog — so
  `switch -c child parent` and `checkout -b child origin/parent` record the parent they really
  forked from, not whatever `@{-1}` happens to name (D34). Installed globally alongside
  pre-push; the shim delegates to any repo-local post-checkout.
- **post-commit** (`_crux-hook post-commit`) — D27: reads the new commit's message; when it is
  human-typed (no `Co-Authored-By: … Claude`, no `Amended-by: Crux`) and in scope, spawns
  `crux enrich-commit --sha <head>` detached. All slow/risky work (LLM call, guarded amend)
  happens in that detached run; the hook itself never blocks or fails the commit. The shim
  delegates to any repo-local post-commit first.
- **Claude Code Stop hook** (`_crux-hook claude-stop`, logic in `crux/claude_intent.py`) —
  registered in `settings.json` as the `_crux-hook` console command (not a `python3 <path>`, so
  it needs no `python3` on PATH). Reads hook JSON on stdin, extracts the session's last
  assistant message from `transcript_path`, writes/merges `.crux/intent.json`:
  `{"ts", "session_id", "summary", "uncertainties": []}`.

## Incremental rules (D9)

RunState persists hunks' fingerprints (file + normalized patch hash), claims, nodes, edges,
items, rendered card. Next run: recompute harvest fresh (free); nodes whose fingerprints are
unchanged reuse previous title/why/questions verbatim and are marked `reused` in the prompt so
the LLM only writes annotations for new/changed nodes. Base SHA change ⇒ cache dropped.

## Roadmap

- **MVP (now):** everything above; GREEN proofs limited to mechanical-cluster identity +
  generated-path detection.
- **v1:** lockfile regeneration byte-compare, rename-completeness proof, spot-check promotion,
  `crux post-review` (checkbox state → draft review), stale-card detector.
- **v2:** run PR's own tests against base commit (exposes tests-written-to-pass),
  split-proposal when RED > 300 effective LOC, triage-miss postmortems feeding signal weights.
