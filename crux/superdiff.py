# Copyright © 2026 Sonomos, Inc.
# SPDX-License-Identifier: Apache-2.0

"""D37: one combined diff per repo, computed without touching anything.

A super PR is reviewed as a single change, so the analysis needs the *end
state* the bundle produces — not N separate diffs stitched back together. This
module builds that end state per repo by merging the member PR heads together
and diffing the result against the base.

It does so with `git merge-tree --write-tree`, which "does not read from or
write to either the working tree or index". Nothing is checked out, no branch
is created, no worktree is added, and nothing is ever pushed. The user can be
mid-edit on any of these clones and this still runs. The merged commits are
loose objects that git garbage-collects on its own.

Conflicts are findings, not failures (D37): a PR that will not combine is
reported with the files it collides on and left OUT of the combined diff, so
the rest of the bundle is still analyzed and still useful. A bundle whose PRs
fight each other is exactly the thing a reviewer most needs told.
"""
from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from crux.gitio import GitError, _try_git, diff_hunks, git_version, run_git
from crux.models import BundleMember, CruxError, Hunk, RepoInfo

log = logging.getLogger("crux.superdiff")

# `git merge-tree --write-tree` — the in-memory merge everything here rests on
# — arrived in git 2.38 (October 2022). Ubuntu and Pop!_OS 22.04 still ship
# 2.34, whose merge-tree is the old three-argument form: every repo failed
# with git's usage text and nothing said the fix was a newer git.
MIN_GIT = (2, 38)

# PR heads are fetched into a private ref namespace so they can never collide
# with a user's own branches or with origin's remote-tracking refs.
_REF_NS = "refs/crux/super"


class SuperDiffError(CruxError):
    """Raised when a repo's combined diff cannot be computed at all."""


def _too_old(have: str) -> SuperDiffError:
    """The one message for a git too old to merge in memory — with the fix."""
    need = ".".join(map(str, MIN_GIT))
    return SuperDiffError(
        f"super PRs need git {need} or newer (for `git merge-tree "
        f"--write-tree`), and this machine has {have}. Upgrade git, then "
        f"retry — on Ubuntu / Pop!_OS 22.04: `sudo add-apt-repository "
        f"ppa:git-core/ppa && sudo apt update && sudo apt install git`; on "
        f"macOS: `brew install git`; on Windows: `winget upgrade Git.Git`")


def require_git() -> None:
    """Refuse up front, once, when git cannot do what this module needs.

    Checked before any repo is touched, because the failure is the machine's,
    not any one repo's: reported per repo it read as N unrelated breakages,
    each ending in git's own usage text. An unreadable version passes — the
    merge itself then fails, and `_merge_tree` still names the cause.
    """
    have = git_version()
    if have is not None and have[:2] < MIN_GIT:
        raise _too_old(f"git {'.'.join(map(str, have))}")


@dataclass
class Conflict:
    """One PR that would not combine with what was already merged.

    `against` distinguishes the two very different things this can mean, and
    the report must not conflate them:

      * **empty** — the PR conflicts with its own base branch. It is simply
        stale and needs a rebase; nothing about the bundle caused this, and it
        would fail to merge on its own too.
      * **non-empty** — the PR collides with sibling PRs already merged into
        the combined diff. This is a genuine bundle finding: two changes in
        flight that cannot both land as written, which no per-PR review can
        see.
    """
    owner: str
    repo: str
    pr: int
    branch: str
    files: list[str] = field(default_factory=list)
    against: list[int] = field(default_factory=list)

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}"

    @property
    def with_base(self) -> bool:
        """True when this is a stale-branch conflict, not a sibling collision."""
        return not self.against


@dataclass
class RepoDiff:
    """The combined change one repo contributes to the bundle."""
    owner: str
    repo: str
    root: str
    base_sha: str
    head_sha: str  # the synthetic merged commit; equals base_sha if all failed
    members: list[BundleMember] = field(default_factory=list)  # actually merged
    conflicts: list[Conflict] = field(default_factory=list)
    hunks: list[Hunk] = field(default_factory=list)
    default_branch: str = "main"

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}"

    def info(self) -> RepoInfo:
        """A RepoInfo over the merged end state, for the normal harvest path.

        `branch` is descriptive only — no such branch exists — but every
        consumer that matters (diff_hunks, blast, history, permalinks) reads
        root and the two SHAs.
        """
        return RepoInfo(
            root=self.root, branch=f"crux/super/{self.repo}",
            head_sha=self.head_sha, base_sha=self.base_sha,
            owner=self.owner, repo=self.repo,
            default_branch=self.default_branch)


def clone_cache() -> Path:
    return Path.home() / ".cache" / "crux" / "clones"


def ensure_clone(owner: str, repo: str, local_path: str | None) -> str:
    """The working directory to run git in for this repo.

    Prefers the user's own clone (already has the objects and the history that
    `harvest/history.py` reads). Falls back to a blobless cache clone, so a
    bundle can include a repo that is not checked out on this machine.
    """
    if local_path and Path(local_path, ".git").exists():
        return local_path
    dest = clone_cache() / f"{owner}__{repo}"
    if (dest / ".git").exists():
        return str(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    log.info("cloning %s/%s (no local clone found)", owner, repo)
    try:
        run_git(["clone", "--filter=blob:none", "--no-checkout",
                 f"https://github.com/{owner}/{repo}.git", str(dest)])
    except GitError as exc:
        raise SuperDiffError(f"{owner}/{repo}: no local clone and cloning failed: {exc}")
    return str(dest)


def _fetch_heads(root: str, owner: str, repo: str, base: str,
                 prs: list[int]) -> None:
    """Fetch the base branch and every member PR head in ONE call.

    PR heads are `refs/pull/<n>/head` — fetchable whether or not the branch
    still exists on a fork, and stable regardless of what the contributor
    renames locally.
    """
    refspecs = [f"+refs/heads/{base}:refs/remotes/origin/{base}"]
    refspecs += [f"+refs/pull/{n}/head:{_REF_NS}/{n}" for n in prs]
    try:
        run_git(["fetch", "--quiet", "origin", *refspecs], cwd=root)
    except GitError as exc:
        raise SuperDiffError(f"{owner}/{repo}: could not fetch PR heads: {exc}")


def _merge_tree(root: str, acc: str, head: str) -> tuple[str, list[str]]:
    """Merge *head* into *acc* in memory.

    Returns (tree_oid, conflicted_files). A non-empty file list means the merge
    conflicted and the tree must not be used.

    merge-tree signals a conflict with exit 1 while still writing useful output
    on stdout, so this reads the process directly rather than going through
    run_git (which raises on any nonzero exit and would discard it). Output
    shape: the tree oid, then one conflicted path per line, then a blank line
    and human-readable messages.
    """
    try:
        proc = subprocess.run(
            ["git", "merge-tree", "--write-tree", "--name-only", acc, head],
            cwd=root, capture_output=True, encoding="utf-8", errors="replace")
    except OSError as exc:
        raise SuperDiffError(f"could not run git merge-tree: {exc}") from exc

    lines = proc.stdout.rstrip("\n").split("\n")
    tree = lines[0].strip() if lines else ""
    if proc.returncode == 0:
        return tree, []
    if "usage: git merge-tree" in proc.stderr:
        # An old git that slipped past `require_git` (its version unreadable):
        # say what is wrong instead of passing on git's usage text.
        raise _too_old("a git without `merge-tree --write-tree`")
    if proc.returncode != 1:
        # 2+ is a real error (bad object, unmerged index), not a conflict.
        raise SuperDiffError(
            f"git merge-tree failed with code {proc.returncode}: "
            f"{proc.stderr.strip() or 'unknown error'}")
    files: list[str] = []
    for line in lines[1:]:
        if not line.strip():
            break  # blank line ends the file list; messages follow
        files.append(line.strip())
    return "", files or ["(unknown)"]


def build(owner: str, repo: str, members: list[BundleMember],
          local_path: str | None = None,
          default_branch: str = "main") -> RepoDiff:
    """Combine this repo's member PRs into one diff against their base."""
    root = ensure_clone(owner, repo, local_path)
    base = members[0].base if members and members[0].base else default_branch
    _fetch_heads(root, owner, repo, base, [m.pr for m in members if m.pr])

    base_sha = _try_git(["rev-parse", f"refs/remotes/origin/{base}"], cwd=root)
    if not base_sha:
        raise SuperDiffError(f"{owner}/{repo}: base branch {base!r} not found on origin")

    result = RepoDiff(owner=owner, repo=repo, root=root, base_sha=base_sha,
                      head_sha=base_sha, default_branch=default_branch)

    acc = base_sha
    landed: list[int] = []
    # Oldest PR first: the earliest work is the foundation the rest builds on,
    # so a later PR conflicting with an earlier one is reported against the
    # earlier one rather than the other way round.
    for member in sorted(members, key=lambda m: m.pr):
        head = _try_git(["rev-parse", f"{_REF_NS}/{member.pr}"], cwd=root)
        if not head:
            result.conflicts.append(Conflict(
                owner=owner, repo=repo, pr=member.pr, branch=member.branch,
                files=["(PR head could not be fetched)"], against=list(landed)))
            continue
        member.head_sha = head
        tree, conflicted = _merge_tree(root, acc, head)
        if conflicted:
            result.conflicts.append(Conflict(
                owner=owner, repo=repo, pr=member.pr, branch=member.branch,
                files=conflicted, against=list(landed)))
            continue
        try:
            acc = run_git(
                ["commit-tree", tree, "-p", acc, "-p", head,
                 "-m", f"crux super: {owner}/{repo}#{member.pr}"], cwd=root)
        except GitError as exc:
            raise SuperDiffError(f"{owner}/{repo}: could not build merged commit: {exc}")
        landed.append(member.pr)
        result.members.append(member)

    result.head_sha = acc
    if acc != base_sha:
        result.hunks = diff_hunks(result.info())
    return result


def build_all(members: list[BundleMember],
              paths: dict[str, str] | None = None,
              defaults: dict[str, str] | None = None,
              ) -> tuple[list[RepoDiff], list[str]]:
    """Combined diffs for every repo in the bundle, plus per-repo problems.

    A repo that cannot be diffed at all degrades to a problem line: the bundle
    still reports on the repos that worked, matching the land-what-can posture
    of the merge itself. A git too old to diff ANY of them raises instead, once
    (`require_git`) — that is not a finding about the bundle.
    """
    require_git()
    paths = paths or {}
    defaults = defaults or {}
    by_repo: dict[str, list[BundleMember]] = {}
    for member in members:
        by_repo.setdefault(f"{member.owner}/{member.repo}", []).append(member)

    out: list[RepoDiff] = []
    problems: list[str] = []
    for slug, group in by_repo.items():
        owner, _, repo = slug.partition("/")
        try:
            out.append(build(owner, repo, group,
                             local_path=paths.get(slug.lower()),
                             default_branch=defaults.get(slug.lower(), "main")))
        except CruxError as exc:
            problems.append(str(exc))
    return out, problems
