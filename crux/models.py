# Copyright © 2026 Sonomos, Inc.
# SPDX-License-Identifier: Apache-2.0

"""Shared data model for Crux. All modules build against these types.

This file is the contract for the parallel build: do not change signatures here
without updating DESIGN.md's module-contract table.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from enum import Enum

STATE_VERSION = 1
CARD_MARKER = "<!-- crux:card -->"
# Second sticky comment: human integration-test steps for the PR's main feature.
TEST_MARKER = "<!-- crux:integration-test -->"
# D37: the sticky super-PR brief, filed as an issue in the group's home repo.
SUPER_MARKER = "<!-- crux:super -->"
# D37: set on the environment of the pushes `crux super` makes, and read by the
# pre-push hook body, which then stands down. A super PR is ONE change — one
# review pass over the combined diff, one Slack message, and its own prompts —
# so a push made on its behalf must not also start the single-repo flow. Lives
# here, not in crux/superpr.py, so the hook can check it without importing the
# whole super stack on every push.
SUPER_ENV = "CRUX_SUPER"
# D37: the one-line pointer left on each member PR, back to that brief.
SUPER_LINK_MARKER = "<!-- crux:super-link -->"
# D41: the `merge_method` values GitHub's merge endpoint accepts. Here rather
# than in crux/supermerge.py because the bundle store validates a brief's
# state block against it, and the store must not import the merge machinery.
MERGE_METHODS = ("merge", "squash", "rebase")

# D40: the sticky comment naming the Zenhub tickets a PR (or a bundle's brief)
# is going to close. A COMMENT, not the PR description: sync_pr_metadata
# rewrites the description from the commits on every push, so a line appended
# there would not survive the next one.
ZEN_MARKER = "<!-- crux:zenhub -->"

# D15: reviewer-facing text must never contain tool jargon. analyze retries the
# LLM once when these appear; tests assert rendered cards contain zero hits.
BANNED_JARGON: tuple[str, ...] = (
    r"\bhunks?\b",
    r"\bDAG\b",
    r"\bentailments?\b",
    r"\btopological(?:ly)?\b",
    r"\bblast[ -]radius\b",
    r"\bfingerprints?\b",
    r"\bupsert(?:s|ed|ing)?\b",
    r"\bmerge[ -]?base\b",
)


def jargon_hits(text: str) -> list[str]:
    """Return the banned-jargon terms found in *text* (D15), if any."""
    hits: list[str] = []
    for pattern in BANNED_JARGON:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            hits.append(match.group(0))
    return hits


class HunkClass(str, Enum):
    BEHAVIORAL = "behavioral"
    MECHANICAL = "mechanical"  # repeated/identical edits, rename threading
    GENERATED = "generated"    # lockfiles, snapshots, build artifacts
    COSMETIC = "cosmetic"      # formatting, comments, whitespace


class Tier(str, Enum):
    RED = "red"
    YELLOW = "yellow"
    GREEN = "green"


class Badge(str, Enum):
    CODE_CHANGE = "CODE CHANGE"
    DESIGN_DECISION = "DESIGN DECISION"
    CODE_CHANGE_EFFECTS = "CODE CHANGE EFFECTS"
    MECHANICAL_CHANGES = "MECHANICAL CHANGES"


class Verdict(str, Enum):
    VERIFIED = "VERIFIED"
    COULD_NOT_VERIFY = "COULD NOT VERIFY"
    CONTRADICTED = "CONTRADICTED"


@dataclass
class RepoInfo:
    root: str
    branch: str
    head_sha: str
    base_sha: str
    owner: str
    repo: str
    default_branch: str = "main"
    # The branch this branch was created from, recorded silently by the
    # post-checkout hook as `git config branch.<name>.cruxBase`. Drives both
    # the diff base (merge-base with it) and the PR base. None when unknown
    # (branch predates the hook, or created some other way) => fall back to
    # the default branch.
    crux_base: str | None = None
    # The branch base_sha was actually resolved against — the candidate that
    # won in repo_info, or the PR's real base once gitio.with_base has aligned
    # the review with it (D34). crux_base says where the branch CAME FROM;
    # this says what the diff is AGAINST, and they differ whenever the PR
    # targets something else. None when nothing resolved (base_sha == head).
    base_branch: str | None = None


@dataclass
class Hunk:
    id: str  # "<file>:<new_start>"
    file: str
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    patch: str  # raw hunk text including context lines
    enclosing_symbol: str | None = None
    klass: HunkClass = HunkClass.BEHAVIORAL

    @property
    def added_lines(self) -> list[str]:
        return [l[1:] for l in self.patch.splitlines() if l.startswith("+")]

    @property
    def removed_lines(self) -> list[str]:
        return [l[1:] for l in self.patch.splitlines() if l.startswith("-")]


def fingerprint(hunk: Hunk) -> str:
    """D9 incremental fingerprint: sha1 of file + normalized patch.

    This is THE one canonical fingerprint: cli.py uses it when saving
    RunState.fingerprints and analyze._split_reused uses it when comparing
    against them — they must always agree or annotation reuse never fires.
    Normalization drops @@ header lines (pure line-number drift must not
    invalidate cached annotations) and trailing whitespace.
    """
    lines = [l.rstrip() for l in hunk.patch.splitlines() if not l.startswith("@@")]
    payload = hunk.file + "\n" + "\n".join(lines)
    return hashlib.sha1(payload.encode("utf-8", "replace")).hexdigest()


@dataclass
class HunkSignals:
    hunk_id: str
    defines: list[str] = field(default_factory=list)
    uses: list[str] = field(default_factory=list)
    blast_radius: int = 0
    callers: list[str] = field(default_factory=list)  # "path:line" samples, max 8
    sensitive: list[str] = field(default_factory=list)  # matched rule names
    churn: int = 0
    fix_frequency: int = 0
    co_change_miss: list[str] = field(default_factory=list)
    test_touched: bool = False
    score: float = 0.0


@dataclass
class Claim:
    id: str  # "C1"
    text: str  # falsifiable behavior sentence
    hunk_ids: list[str]
    kind: str = "behavior"  # behavior | decision | contract
    uncertain: bool = False  # author-agent flagged unsure


@dataclass
class DagNode:
    number: int  # shared numbering: mermaid label AND item number
    title: str  # short human name, e.g. "WriteBuffer class"
    hunk_ids: list[str]
    badge: Badge
    reused: bool = False  # annotation carried over from previous run


@dataclass
class DagEdge:
    src: int
    dst: int
    reason: str  # symbol that links cause to effect


@dataclass
class AuditRow:
    claim: str
    verdict: Verdict
    evidence: str  # human sentence, should reference file:line


@dataclass
class DesignFinding:
    """D14: an OO-design / coding-standards finding. Evidence contract: a finding
    without a convention_ref citation must be dropped by the validator."""
    kind: str  # "oo-design" | "duplicate-helper" | "convention"
    text: str  # one-two sentences, concrete
    file: str
    line_start: int
    line_end: int
    convention_source: str  # "CLAUDE.md" | "crux.toml" | "sibling code"
    convention_ref: str  # the rule text, or file:line of the sibling/helper it diverges from
    permalink: str = ""


@dataclass
class Memory:
    """D31: one durable plain-English fact Crux knows about a repo, read into
    every future review prompt. Lives in the per-repo store managed by
    crux/memory.py (`crux memory` on the CLI)."""
    id: str  # short content hash of the normalized text — stable across runs
    text: str  # one plain sentence
    anchor: str = ""  # "path" or "path:line" that proves it; "" = repo-wide
    source: str = "review"  # "review" (LLM-proposed) | "human" (crux memory add)
    created: str = ""  # ISO date


@dataclass
class MemoryRetraction:
    """D36: one remembered fact a review believes its PR has just made false.
    Only a request — memory.absorb decides whether the fact actually goes (a
    human-added one never does), and the reason ends up in the log, never on
    the card."""
    id: str  # the [id] the prompt showed for that fact
    why: str = ""  # one sentence: what in this PR contradicts it


@dataclass
class MapStep:
    """D33: one box of the change map — a stage of what happens, named the way
    someone USING the software would name it, never a file or symbol."""
    id: str  # short slug the arrows refer to; unique within the map
    label: str  # at most 5 plain words


@dataclass
class MapArrow:
    src: str  # MapStep.id
    dst: str  # MapStep.id
    label: str = ""  # short "when trivial"-style note; "" when self-evident


@dataclass
class ChangeMap:
    """D33: the one picture on the card — the PR's flow, end to end, at the
    level a person experiences it. Written by the review, not derived from the
    code graph: the DAG still numbers items and sets reading order, but it no
    longer reaches the reviewer as a diagram."""
    steps: list[MapStep] = field(default_factory=list)
    arrows: list[MapArrow] = field(default_factory=list)


@dataclass
class NodeAnnotation:
    number: int
    title: str  # falsifiable phrasing for RED items
    why: str
    questions: list[str] = field(default_factory=list)
    chips: list[str] = field(default_factory=list)
    minutes: int = 2
    design_decision: bool = False
    # LLM tier opinion (pipeline step 5): "" or "red"|"yellow"|"green".
    # It can only RAISE an item above its rule floor, never lower it.
    suggested_tier: str = ""
    # D24: when the change rewrites/moves existing behavior, one short plain
    # sentence each — Crux states the comparison so the reviewer never has to
    # diff versions themself. Both empty for brand-new code.
    before: str = ""
    after: str = ""
    # D23: for large changes, 2-6 analysis bullets, each one plain sentence
    # ending with a `path:a-b` range of at most Config.max_read_lines lines.
    breakdown: list[str] = field(default_factory=list)
    # D32: True when the model returned NO entry for this change — the prompt
    # says omitting one marks it routine, so the title here is machine-made and
    # there is no analysis behind it. Only analyze's default-fill sets this;
    # the --no-llm path leaves it False (no model, so nothing was omitted).
    omitted_by_model: bool = False


@dataclass
class Annotation:
    summary: str  # one-sentence intent of the PR
    # A concise, human-readable PR title synthesized from the change (a few
    # words, not the branch name). Empty on --no-llm; the PR keeps its fast
    # commit-based title then.
    pr_title: str = ""
    # 2-4 high-level, plain-English bullets: the big ideas of the PR and how the
    # parts relate. This is what leads the card; the per-node detail is demoted.
    overview: list[str] = field(default_factory=list)
    # D33: the card's change map — a handful of user-level steps and the arrows
    # between them. None (or under two steps) means the PR had no flow worth
    # drawing and the card simply shows no diagram.
    change_map: ChangeMap | None = None
    # Minimal, copy-pasteable steps a human runs to verify the PR's BIGGEST
    # functionality end-to-end. Empty unless the PR ships something worth
    # exercising by hand (not refactors/docs/config/tests, not unit tests).
    integration_test: list[str] = field(default_factory=list)
    claims: list[Claim] = field(default_factory=list)
    nodes: dict[int, NodeAnnotation] = field(default_factory=dict)
    audit: list[AuditRow] = field(default_factory=list)
    design: list[DesignFinding] = field(default_factory=list)  # D14, capped at Config.standards_max
    # D31: durable repo facts this review proposed remembering. Absorbed into
    # the per-repo store by cli.run AFTER the card posts (never on --dry-run).
    memories: list[Memory] = field(default_factory=list)
    # D36: remembered facts this review believes the PR made false, retired on
    # the same pass and under the same conditions as the additions above.
    forget_memories: list[MemoryRetraction] = field(default_factory=list)


@dataclass
class Item:
    number: int
    tier: Tier
    badge: Badge
    title: str
    file: str
    line_start: int
    line_end: int
    minutes: int
    chips: list[str] = field(default_factory=list)
    why: str = ""
    questions: list[str] = field(default_factory=list)
    permalink: str = ""
    diff_link: str = ""
    deleted: bool = False  # file gone at head: links must use the base-side blob
    # Changed lines (added + removed) in this item's chunks. The card header
    # sums these per tier so its numbers always add up (verify finding #1).
    changed_lines: int = 0
    # D24: Crux's own before/now comparison for rewritten/moved behavior.
    before: str = ""
    after: str = ""
    # D23: sub-parts shown when the item's span exceeds Config.max_read_lines —
    # each a plain sentence ending in a `path:a-b` pointer of at most that many
    # lines. LLM-written when available, else split mechanically per chunk.
    breakdown: list[str] = field(default_factory=list)
    # D32: this item is on the card because a rule floor flagged it, not because
    # the review had anything to say — its title is machine-made and its why is
    # empty. Such an item is never must-read; the card shows the rule that
    # flagged it instead of an empty line.
    no_analysis: bool = False


@dataclass
class SuperCheck:
    """D37: one line of the brief's "check before merging" list.

    The whole per-change apparatus of a normal card (tiers, badges, breakdowns,
    before/now) is deliberately absent. A super PR brief is not a review of
    every change in the bundle — the member PRs already carry those. It answers
    the one question no per-PR review can: across all this work, what must a
    human look at before it lands?
    """
    text: str  # one sentence, <=20 words
    anchor: str = ""  # "owner/repo path/file.py:line" — rendered as a link
    prs: list[str] = field(default_factory=list)  # "owner/repo#12" refs


@dataclass
class SuperAnnotation:
    """D37: the single LLM pass over the whole bundle.

    One call, not one per PR: the combined diff goes in, the cross-cutting
    meaning comes out. Analyzing each PR separately and summarizing the
    summaries is precisely what this replaces — it costs N times as much and
    still cannot see what only shows up when the changes sit together.
    """
    thesis: str = ""  # one sentence, <=25 words: what the bundle does
    ideas: list[str] = field(default_factory=list)  # spanning ideas, capped
    change_map: ChangeMap | None = None
    checks: list[SuperCheck] = field(default_factory=list)
    # Merge order across repos as "owner/repo#pr", plus one sentence of why.
    order: list[str] = field(default_factory=list)
    order_why: str = ""
    # D37: the same minimal hand-verification the per-PR card offers, asked at
    # the bundle's level — the walkthrough that crosses repos is the one no
    # member PR can give, and usually the only way to know the feature works.
    integration_test: list[str] = field(default_factory=list)


@dataclass
class LocalClone:
    """A clone of an in-scope repo found on this machine. The picker reads its
    working state directly — no network — so candidates list instantly."""
    path: str
    owner: str
    repo: str
    branch: str
    head_sha: str = ""
    # Epoch seconds of the branch tip; the picker sorts on this, newest first.
    last_commit_ts: int = 0
    default_branch: str = "main"


@dataclass
class Candidate:
    """One numbered line in the picker: a branch that could join a super PR,
    whether or not it has a PR yet (D37 opens one on selection)."""
    owner: str
    repo: str
    branch: str
    last_commit_ts: int = 0
    pr: int | None = None  # existing open PR, else None => created on selection
    title: str = ""
    path: str = ""  # local clone path; "" for a PR with no clone on this machine
    # Display name of whoever opened the PR ("Firstname Lastname"), taken from
    # the PR list Crux already fetches — so crediting costs no extra API call.
    author: str = ""
    # Set when the branch is already part of another super PR — such a
    # candidate is filtered out of the picker, never silently double-booked.
    bundled_in: int | None = None

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}"


@dataclass
class BundleMember:
    owner: str
    repo: str
    branch: str
    pr: int
    head_sha: str = ""
    base: str = "main"
    author: str = ""  # display name, for crediting the bundle in Slack
    # Filled by the merge (D37): "merged" | "blocked" | "" (not attempted).
    state: str = ""
    # Why this member could not land, in plain words, when state == "blocked".
    error: str = ""


@dataclass
class Bundle:
    """D37: the super PR itself — a cross-repo set of PRs reviewed as one
    change and merged in one call. GitHub has no cross-repo grouping (stacks
    are `/repos/{owner}/{repo}/stacks`, single-repo by construction), so Crux
    owns this state and the one-PR-one-bundle rule that goes with it."""
    number: int
    # Human label for the bundle, shown in `crux super list` and the issue
    # title. Free text: there is no configured set of names to choose from —
    # a super PR is whatever PRs you picked. Defaults to a member's branch.
    name: str = ""
    members: list[BundleMember] = field(default_factory=list)
    home: str = ""  # "owner/name" holding the issue
    issue: int | None = None
    created_at: str = ""
    updated_at: str = ""
    # Merge order Crux computed across repos, as "owner/repo#pr" strings.
    order: list[str] = field(default_factory=list)
    # D16/D37: the bundle's Slack announcement, so every later refresh and the
    # merge report reply in ONE thread instead of posting to the channel again.
    slack_channel: str = ""
    slack_ts: str = ""
    # D38: set when the brief was closed from the card's Close button, so the
    # bundle drops out of `crux super list` without its number being reused.
    closed: bool = False
    # D37/D38: the last brief's hand-verification steps, kept here so the
    # machine that just checked every repo out can show them immediately —
    # the person who pressed "set up to test" wants the steps, not a trip back
    # to the issue to find them.
    test_steps: list[str] = field(default_factory=list)
    # D38: bumped by every save, carried in the brief's state block, and the
    # only thing that can tell a teammate's CACHED copy from a newer published
    # one. Not a timestamp: the copies live on different machines, `save`
    # restamps `updated_at` on write (so a cache write looks newer than the
    # brief it came from), and clock skew between two laptops is not something
    # correctness should rest on. A counter only ever has to be compared.
    rev: int = 0
    # D41: True once a human has decided `order` (`crux super order`). Every
    # re-brief — including the automatic one a member push triggers — then
    # keeps it instead of taking the review pass's; members added later are
    # appended, removed ones drop out. False is the D37 behaviour.
    order_pinned: bool = False
    # D41: how this bundle's PRs are merged — "merge" | "squash" | "rebase", or
    # "" for "not set here" (then `[super] merge_method`, then squash). Carried
    # in the brief's state block, so a teammate's Merge button honours it.
    merge_method: str = ""
    # D41: what the review pass last proposed, kept apart from `order` so a
    # pinned brief can still show the model's reasoning, and `--unpin` can hand
    # its order back without another model call.
    suggested_order: list[str] = field(default_factory=list)
    order_why: str = ""


@dataclass
class ZenTicket:
    """D40: one Zenhub ticket, in the terms Crux needs to name and close it.

    `id` is Zenhub's own node ID and the only thing its mutations accept;
    everything else is what a human recognises the ticket by. Both are kept
    because the ID cannot be shown to a person and the number cannot be sent
    to the API.
    """
    id: str = ""
    number: int = 0          # the GitHub issue number
    owner: str = ""          # the GitHub repo the issue lives in
    repo: str = ""
    title: str = ""
    body: str = ""           # the description, for the picker to show
    url: str = ""
    pipeline: str = ""       # Zenhub pipeline name, when the query carried one
    kind: str = ""           # Zenhub issue type: "Task", "Bug", "Epic", ...
    planning: bool = False   # an Epic/Project/Initiative — never linked or closed


@dataclass
class ZenLink:
    """D40: the tickets one PR — or one whole bundle — is going to close.

    A single key covers both shapes deliberately. A regular PR is
    ``pr:owner/repo#12``; a super PR is ``super:7``, whose tickets close only
    once EVERY member PR has landed, because a cross-repo ticket is not done
    while half its repos are unmerged. `prs` is what must land, so the sync
    pass can answer that question without reopening the bundle store.
    """
    key: str
    tickets: list[ZenTicket] = field(default_factory=list)
    prs: list[str] = field(default_factory=list)   # "owner/repo#12"
    created_at: str = ""
    # Set once the tickets have actually been closed, so a re-run of the sync
    # (or a second merge attempt) does not close them twice.
    closed: bool = False


@dataclass
class GateDecision:
    skip: bool
    reasons: list[str]
    stats: dict = field(default_factory=dict)  # behavioral_lines, total_lines, etc.


@dataclass
class Config:
    # GitHub owners (orgs/users) Crux acts on (D13). No built-in default:
    # empty means Crux does nothing until [scope] owners is set.
    scope_owners: list[str] = field(default_factory=list)
    # Optional finer-grained allowlist of "owner/name" repos (case-
    # insensitive). Empty means every repo of an allowed owner; non-empty
    # means hooks (git and Claude Code alike) act only on the repos listed
    # here. Set in [scope] repos.
    scope_repos: list[str] = field(default_factory=list)
    # gate
    gate_min_behavioral_lines: int = 50
    gate_max_blast: int = 5
    # sensitivity
    sensitive_paths: list[str] = field(default_factory=lambda: [
        "auth", "billing", "payment", "security", "secrets", "migrations",
        ".github/workflows", "permissions",
    ])
    sensitive_keywords: list[str] = field(default_factory=lambda: [
        "password", "token", "secret", "api_key", "crypto", "subprocess",
        "eval(", "exec(", "DROP TABLE", "DELETE FROM", "permission", "sudo",
    ])
    dependency_files: list[str] = field(default_factory=lambda: [
        "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "uv.lock",
        "Cargo.lock", "go.sum", "poetry.lock", "requirements.txt", "pyproject.toml",
    ])
    generated_globs: list[str] = field(default_factory=lambda: [
        "*.lock", "*.min.js", "*.snap", "*_pb2.py", "*.generated.*", "dist/", "build/",
    ])
    # history
    history_window_days: int = 180
    co_change_threshold: float = 0.7
    # tests
    test_dirs: list[str] = field(default_factory=lambda: ["tests", "test", "__tests__", "spec"])
    # llm
    llm_model: str = "sonnet"
    llm_timeout: int = 600
    # pr
    pr_default_base: str = "main"
    # When true, the pre-push flow creates a missing PR automatically instead
    # of prompting on the terminal (D11). Off by default: a human is asked.
    pr_auto_create: bool = False
    # D39: what to do when the PR head branch is missing from the remote.
    # A branch renamed locally keeps the upstream of the name it had, so
    # `git push` moves the OLD remote branch and the new name never arrives —
    # `gh pr create --head <new-name>` then dies with "Head ref must be a
    # branch". "ask" (default) offers to push it, "always" pushes without
    # asking, "never" leaves it and lets gh surface the failure.
    pr_push_head: str = "ask"
    # The remote a missing head branch is pushed to.
    pr_push_remote: str = "origin"
    # commit (D27): after a human-typed commit (no Claude co-author trailer),
    # rewrite its terse message from the diff and amend the commit in place —
    # before any push — so the PR title/description built from commit
    # subjects (D19) have real material to work from.
    commit_enrich: bool = True
    # prs: `crux prs` fans one `gh pr list` out per repo on a thread pool.
    # 0 = auto: max parallel tasks for the machine (CPU count + 4, capped at
    # 32); a positive number caps the pool there instead.
    prs_jobs: int = 0
    # standards (D14)
    standards_rules: list[str] = field(default_factory=list)
    standards_max: int = 5
    # rendering
    red_loc_cap: int = 300
    comment_char_cap: int = 60000
    # D23: the most lines of code any single card pointer may ask a reviewer
    # to read. Items spanning more get a breakdown into parts of at most this
    # many lines each.
    max_read_lines: int = 50
    # slack (D16): announce PRs to a channel. Empty channel disables it; the
    # bot token is read from the env var named here (never stored in config).
    slack_channel: str = ""
    slack_token_env: str = "SLACK_BOT_TOKEN"
    # memory (D31): the per-repo long-term memory store. Disabled => reviews
    # neither read nor write it (the store and `crux memory` still work).
    memory_enabled: bool = True
    # Most facts kept per repo; review-written facts are evicted oldest-first
    # before any human-added fact is touched.
    memory_max: int = 40
    # super PRs (D37). The candidate repos are the ones already configured
    # above — `scope_repos` when set, else every repo of `scope_owners`.
    # Where super-PR issues are filed ("owner/name").
    super_home: str = ""
    # Directories searched (a few levels deep) for local clones of the
    # in-scope repos. Empty => the parent directory of the repo the command
    # runs in, which is where sibling clones normally live.
    super_roots: list[str] = field(default_factory=list)
    # D37 hard caps for the one-screen brief. These are enforced in render, not
    # merely asked for in the prompt: a bundle of 30 PRs must not produce a
    # longer card than one of 3, or the brief stops being a brief.
    super_ideas_max: int = 5
    super_checks_max: int = 7
    # D41: the merge method for a super PR that does not set its own (`crux
    # super order N --method`). An explicit `crux super merge --method` beats
    # both. Anything but merge/squash/rebase is ignored in favour of squash.
    super_merge_method: str = "squash"
    # zenhub (D40): link tickets to the PRs that implement them and close
    # them when those PRs land. OFF unless `workspace` is set — empty means
    # Crux makes no Zenhub calls and prompts for nothing, which is the
    # default. Name or ID; the key is never stored in config.
    zenhub_workspace: str = ""
    zenhub_token_env: str = "ZENHUB_API_KEY"
    # Whether landing a PR closes its linked tickets. False keeps the linking
    # (and `crux zenhub sync`) while leaving the closing to a human.
    zenhub_close_on_merge: bool = True
    # Pipeline a closed ticket is moved to. Empty = close without moving.
    zenhub_done_pipeline: str = ""
    # Whether a review offers the ticket picker on the terminal. False links
    # only when asked by hand (`crux zenhub link`) — for people who want the
    # closing without being asked on every push.
    zenhub_ask: bool = True
    # D38: the loopback port `crux serve` listens on, and the port the brief's
    # action links point at. Same number in everyone's card, because 127.0.0.1
    # resolves on the machine of whoever clicks — that is the whole mechanism.
    # 0 disables the links (the brief simply carries none).
    serve_port: int = 8787


@dataclass
class RunState:
    version: int
    branch: str
    base_sha: str
    head_sha: str
    pr_number: int | None
    fingerprints: dict[str, str]  # hunk_id -> sha1 of normalized patch
    claims: list[Claim] = field(default_factory=list)
    nodes: list[DagNode] = field(default_factory=list)
    edges: list[DagEdge] = field(default_factory=list)
    annotation: Annotation | None = None
    items: list[Item] = field(default_factory=list)
    card: str = ""
    generated_at: str = ""
    skipped: bool = False
    # Slack: the channel + message timestamp of this PR's announcement, so the
    # next push threads a reply under it instead of posting a new message.
    slack_channel: str = ""
    slack_ts: str = ""


# ---------------------------------------------------------------------------
# JSON (de)serialization for RunState — used by crux/cache.py
# ---------------------------------------------------------------------------

def state_to_json(state: RunState) -> str:
    return json.dumps(asdict(state), indent=1, default=str)


def state_from_json(text: str) -> RunState:
    raw = json.loads(text)
    ann = raw.get("annotation")
    annotation = None
    if ann:
        raw_map = ann.get("change_map") or {}
        annotation = Annotation(
            summary=ann.get("summary", ""),
            pr_title=ann.get("pr_title", ""),
            overview=list(ann.get("overview", [])),
            change_map=ChangeMap(
                steps=[MapStep(**s) for s in raw_map.get("steps", [])],
                arrows=[MapArrow(**a) for a in raw_map.get("arrows", [])],
            ) if raw_map else None,
            integration_test=list(ann.get("integration_test", [])),
            claims=[Claim(**c) for c in ann.get("claims", [])],
            nodes={int(k): NodeAnnotation(**v) for k, v in (ann.get("nodes") or {}).items()},
            audit=[AuditRow(claim=a["claim"], verdict=Verdict(a["verdict"]), evidence=a["evidence"])
                   for a in ann.get("audit", [])],
            design=[DesignFinding(**d) for d in ann.get("design", [])],
            memories=[Memory(**m) for m in ann.get("memories", [])],
            forget_memories=[MemoryRetraction(**m)
                             for m in ann.get("forget_memories", [])],
        )
    return RunState(
        version=raw["version"],
        branch=raw["branch"],
        base_sha=raw["base_sha"],
        head_sha=raw["head_sha"],
        pr_number=raw.get("pr_number"),
        fingerprints=raw.get("fingerprints", {}),
        claims=[Claim(**c) for c in raw.get("claims", [])],
        nodes=[DagNode(number=n["number"], title=n["title"], hunk_ids=n["hunk_ids"],
                       badge=Badge(n["badge"]), reused=n.get("reused", False))
               for n in raw.get("nodes", [])],
        edges=[DagEdge(**e) for e in raw.get("edges", [])],
        annotation=annotation,
        items=[Item(number=i["number"], tier=Tier(i["tier"]), badge=Badge(i["badge"]),
                    title=i["title"], file=i["file"], line_start=i["line_start"],
                    line_end=i["line_end"], minutes=i["minutes"], chips=i.get("chips", []),
                    why=i.get("why", ""), questions=i.get("questions", []),
                    permalink=i.get("permalink", ""), diff_link=i.get("diff_link", ""),
                    deleted=i.get("deleted", False),
                    changed_lines=i.get("changed_lines", 0),
                    before=i.get("before", ""), after=i.get("after", ""),
                    breakdown=i.get("breakdown", []),
                    no_analysis=i.get("no_analysis", False))
               for i in raw.get("items", [])],
        card=raw.get("card", ""),
        generated_at=raw.get("generated_at", ""),
        skipped=raw.get("skipped", False),
        slack_channel=raw.get("slack_channel", ""),
        slack_ts=raw.get("slack_ts", ""),
    )


class CruxError(Exception):
    """Base for all Crux errors."""


class LlmError(CruxError):
    pass


class NotLoggedInError(LlmError):
    """Raised when the local `claude` CLI is not authenticated (D7). Distinct
    from a generic LlmError so callers can surface an actionable "run `claude
    login`" notice instead of a buried, near-empty "claude exited 1" log line."""


class PostError(CruxError):
    pass
