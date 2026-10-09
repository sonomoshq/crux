# Copyright © 2026 Sonomos, Inc.
# SPDX-License-Identifier: Apache-2.0

"""Tests for crux.gitio against a synthetic git repo built in a tempdir."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from crux import gitio
from crux.models import CruxError, Hunk, RepoInfo

LIB_PY = """\
CONSTANT = 1


def alpha():
    a = 1
    b = 2
    c = 3
    return a


def omega():
    value = CONSTANT
    extra = 2
    return value + extra
"""

LIB_PY_CHANGED = LIB_PY.replace("    b = 2\n", "    b = 20\n").replace(
    "    return value + extra\n", "    return value + extra + 1\n")

DEEP_PY = """\
import os


def deep():
    a = 1
    b = 2
    c = 3
    d = 4
    e = 5
    f = 6
    return a
"""

OLDNAME_PY = """\
def renamed_helper():
    a = 1
    b = 2
    c = 3
    d = 4
    e = 5
    f = 6
    return a
"""

SCANNER_PY = """\
# scanner
import os


def hidden():
    x = 1
    y = 2
    z = 3
    w = 4
    return x
"""


def _git(repo: str, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=repo, check=True,
                          capture_output=True, text=True)
    return proc.stdout.strip()


def _write(repo: str, name: str, content: str | bytes) -> None:
    path = os.path.join(repo, name)
    if isinstance(content, bytes):
        with open(path, "wb") as f:
            f.write(content)
    else:
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)


class RepoFixture(unittest.TestCase):
    """Builds one synthetic repo: base commit on main, edits on feature."""

    tmp: str
    repo: str
    main_sha: str
    head_sha: str

    @classmethod
    def setUpClass(cls) -> None:
        # Hermetic git: ignore the developer's global/system config.
        cls._env = mock.patch.dict(os.environ, {
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
        })
        cls._env.start()
        cls.tmp = tempfile.mkdtemp(prefix="crux-gitio-")
        cls.repo = os.path.join(cls.tmp, "repo")
        os.makedirs(cls.repo)
        _git(cls.repo, "init", "-q", "-b", "main")
        _git(cls.repo, "config", "user.email", "t@example.com")
        _git(cls.repo, "config", "user.name", "Test")

        _write(cls.repo, "lib.py", LIB_PY)
        _write(cls.repo, "deep.py", DEEP_PY)
        _write(cls.repo, "gone.py", "def gone():\n    return 0\n")
        _write(cls.repo, "oldname.py", OLDNAME_PY)
        _write(cls.repo, "scanner.py", SCANNER_PY)
        _write(cls.repo, "blob.bin", b"\x00\x01\x02data")
        _git(cls.repo, "add", "-A")
        _git(cls.repo, "commit", "-qm", "base")
        cls.main_sha = _git(cls.repo, "rev-parse", "HEAD")

        _git(cls.repo, "checkout", "-qb", "feature")
        _write(cls.repo, "lib.py", LIB_PY_CHANGED)
        _write(cls.repo, "deep.py", DEEP_PY.replace("    e = 5\n", "    e = 50\n"))
        _write(cls.repo, "newfile.py", "def fresh():\n    return 42\n")
        os.remove(os.path.join(cls.repo, "gone.py"))
        _git(cls.repo, "mv", "oldname.py", "newname.py")
        _write(cls.repo, "newname.py", OLDNAME_PY.replace("    d = 4\n", "    d = 44\n"))
        _write(cls.repo, "blob.bin", b"\x00\x01\x03DATA!")
        _git(cls.repo, "add", "-A")
        _git(cls.repo, "commit", "-qm", "feature work")
        cls.head_sha = _git(cls.repo, "rev-parse", "HEAD")

        # No fetch ever happens: origin exists only as a URL (offline tests).
        _git(cls.repo, "remote", "add", "origin",
             "git@github.com:example-org/widget.git")

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)
        cls._env.stop()


class TestRunGit(RepoFixture):
    def test_returns_stdout_stripped(self) -> None:
        out = gitio.run_git(["rev-parse", "HEAD"], cwd=self.repo)
        self.assertEqual(out, self.head_sha)
        self.assertFalse(out.endswith("\n"))

    def test_failure_raises_giterror(self) -> None:
        with self.assertRaises(gitio.GitError):
            gitio.run_git(["rev-parse", "--verify", "no-such-ref-xyz"],
                          cwd=self.repo)

    def test_giterror_is_cruxerror(self) -> None:
        self.assertTrue(issubclass(gitio.GitError, CruxError))

    def test_missing_binary_raises_cruxerror(self) -> None:
        with mock.patch("crux.gitio.subprocess.run",
                        side_effect=FileNotFoundError("git")):
            with self.assertRaises(CruxError):
                gitio.run_git(["status"])


class TestGitVersion(unittest.TestCase):
    """The leading dotted number of `git --version`, whatever the build adds."""

    def _version(self, out: str | None):
        with mock.patch.object(gitio, "_try_git", return_value=out):
            return gitio.git_version()

    def test_the_forms_git_prints(self) -> None:
        self.assertEqual(self._version("git version 2.34.1"), (2, 34, 1))
        self.assertEqual(self._version("git version 2.39.3 (Apple Git-146)"),
                         (2, 39, 3))
        self.assertEqual(self._version("git version 2.45.1.windows.1"),
                         (2, 45, 1))
        self.assertEqual(self._version("git version 2.50"), (2, 50, 0))

    def test_unreadable_is_unknown_not_old(self) -> None:
        # No git, or output this does not recognise: callers must not read
        # None as "too old" and refuse a git that would have worked.
        self.assertIsNone(self._version(None))
        self.assertIsNone(self._version("git version unknown"))

    def test_it_asks_the_real_git(self) -> None:
        self.assertIsNotNone(gitio.git_version())


class TestParseOwnerRepo(unittest.TestCase):
    def test_url_forms(self) -> None:
        cases = [
            ("git@github.com:example-org/widget.git", ("example-org", "widget")),
            ("https://github.com/example-org/widget.git", ("example-org", "widget")),
            ("https://github.com/example-org/widget", ("example-org", "widget")),
            ("ssh://git@github.com/example-org/widget.git", ("example-org", "widget")),
            ("https://user@github.com/owner/repo/", ("owner", "repo")),
            ("", ("", "")),
            ("git@github.com:widget.git", ("", "")),
            ("/local/path/only", ("path", "only")),
        ]
        for url, expected in cases:
            with self.subTest(url=url):
                self.assertEqual(gitio._parse_owner_repo(url), expected)

    def test_unquote(self) -> None:
        self.assertEqual(gitio._unquote('"\\303\\251.py"'), "é.py")
        self.assertEqual(gitio._unquote('"tab\\there"'), "tab\there")
        self.assertEqual(gitio._unquote("plain.py"), "plain.py")


class TestRepoInfo(RepoFixture):
    def test_basic_fields(self) -> None:
        info = gitio.repo_info(cwd=self.repo)
        self.assertEqual(info.root, os.path.realpath(self.repo))
        self.assertEqual(info.branch, "feature")
        self.assertEqual(info.head_sha, self.head_sha)
        self.assertEqual(info.owner, "example-org")
        self.assertEqual(info.repo, "widget")
        # origin/HEAD is unset -> default branch falls back to "main",
        # and merge-base falls back to the local main branch.
        self.assertEqual(info.default_branch, "main")
        self.assertEqual(info.base_sha, self.main_sha)

    def test_works_from_subdirectory(self) -> None:
        sub = os.path.join(self.repo, "subdir")
        os.makedirs(sub, exist_ok=True)
        info = gitio.repo_info(cwd=sub)
        self.assertEqual(info.root, os.path.realpath(self.repo))

    def test_default_branch_from_symbolic_ref(self) -> None:
        _git(self.repo, "update-ref", "refs/remotes/origin/develop", self.main_sha)
        _git(self.repo, "symbolic-ref", "refs/remotes/origin/HEAD",
             "refs/remotes/origin/develop")
        try:
            info = gitio.repo_info(cwd=self.repo)
            self.assertEqual(info.default_branch, "develop")
            self.assertEqual(info.base_sha, self.main_sha)
        finally:
            _git(self.repo, "symbolic-ref", "--delete", "refs/remotes/origin/HEAD")
            _git(self.repo, "update-ref", "-d", "refs/remotes/origin/develop")

    def test_base_ref_override(self) -> None:
        info = gitio.repo_info(cwd=self.repo, base_ref="main")
        self.assertEqual(info.base_sha, self.main_sha)

    def test_crux_base_drives_diff_base(self) -> None:
        # branch.<name>.cruxBase (set by the post-checkout hook) is preferred
        # over the default branch: the diff base is the fork point from the
        # recorded parent, not from main.
        tmp = tempfile.mkdtemp(prefix="crux-cruxbase-")
        try:
            repo = os.path.join(tmp, "r")
            os.makedirs(repo)
            _git(repo, "init", "-q", "-b", "main")
            _git(repo, "config", "user.email", "t@example.com")
            _git(repo, "config", "user.name", "Test")
            _write(repo, "a.py", "x = 1\n")
            _git(repo, "add", "-A"); _git(repo, "commit", "-qm", "on main")
            _git(repo, "checkout", "-qb", "parent")
            _write(repo, "b.py", "y = 2\n")
            _git(repo, "add", "-A"); _git(repo, "commit", "-qm", "on parent")
            parent_sha = _git(repo, "rev-parse", "HEAD")
            _git(repo, "checkout", "-qb", "child")
            _write(repo, "c.py", "z = 3\n")
            _git(repo, "add", "-A"); _git(repo, "commit", "-qm", "on child")
            # simulate the post-checkout hook having recorded the parent
            _git(repo, "config", "branch.child.cruxBase", "parent")

            info = gitio.repo_info(cwd=repo)
            self.assertEqual(info.crux_base, "parent")
            # base is the parent fork point, NOT main
            self.assertEqual(info.base_sha, parent_sha)
            files = sorted({h.file for h in gitio.diff_hunks(info)})
            self.assertEqual(files, ["c.py"])  # main-based would also show b.py
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_crux_base_missing_branch_falls_back(self) -> None:
        # A recorded parent that no longer exists must not break repo_info; it
        # falls through to the default-branch base.
        _git(self.repo, "config", "branch.feature.cruxBase", "does-not-exist")
        try:
            info = gitio.repo_info(cwd=self.repo)
            self.assertEqual(info.crux_base, "does-not-exist")
            self.assertEqual(info.base_sha, self.main_sha)
        finally:
            _git(self.repo, "config", "--unset", "branch.feature.cruxBase")

    def test_single_branch_clone_fetches_base(self) -> None:
        """A clone holding only the feature branch (the shape claude.ai/code
        session containers arrive in: no origin/HEAD, no local ref for main)
        must fetch the base from origin rather than collapse to a zero-line
        diff (base_sha == head_sha)."""
        tmp = tempfile.mkdtemp(prefix="crux-singlebranch-")
        try:
            origin = os.path.join(tmp, "origin")
            os.makedirs(origin)
            _git(origin, "init", "-q", "-b", "main")
            _git(origin, "config", "user.email", "t@example.com")
            _git(origin, "config", "user.name", "Test")
            _write(origin, "a.txt", "base\n")
            _git(origin, "add", "-A")
            _git(origin, "commit", "-qm", "base")
            main_sha = _git(origin, "rev-parse", "HEAD")
            _git(origin, "checkout", "-qb", "feature")
            _write(origin, "a.txt", "changed\n")
            _git(origin, "add", "-A")
            _git(origin, "commit", "-qm", "feature work")

            clone = os.path.join(tmp, "clone")
            _git(tmp, "clone", "-q", "--branch", "feature", "--single-branch",
                 origin, clone)
            # Match the cloud-container shape exactly: no origin/HEAD (so the
            # default branch falls back to "main", which has no local ref).
            _git(clone, "remote", "set-head", "origin", "-d")

            info = gitio.repo_info(cwd=clone)
            self.assertEqual(info.default_branch, "main")
            self.assertEqual(info.base_sha, main_sha)
            # The fetched branch is also what the diff is against, so the card
            # and _choose_pr name it (D34) instead of showing nothing.
            self.assertEqual(info.base_branch, "main")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_single_branch_clone_honours_explicit_base(self) -> None:
        """`--base` must win on the single-branch-clone path too.

        Every other path puts base_ref at the head of the candidate list; the
        fetch path took only (cruxBase, default branch), so an explicit
        override was silently dropped in exactly the checkouts that need the
        fetch — and the run then recorded the *default* branch as
        RepoInfo.base_branch, mislabelling the card with a base the user had
        overridden.
        """
        tmp = tempfile.mkdtemp(prefix="crux-basefetch-")
        try:
            origin = os.path.join(tmp, "origin")
            os.makedirs(origin)
            _git(origin, "init", "-q", "-b", "main")
            _git(origin, "config", "user.email", "t@example.com")
            _git(origin, "config", "user.name", "Test")
            _write(origin, "a.txt", "base\n")
            _git(origin, "add", "-A")
            _git(origin, "commit", "-qm", "base")
            main_sha = _git(origin, "rev-parse", "HEAD")
            # release branches off main, feature off release — so the two
            # candidate bases give DIFFERENT merge-bases and the assertion
            # can tell which one was used.
            _git(origin, "checkout", "-qb", "release")
            _write(origin, "a.txt", "release\n")
            _git(origin, "add", "-A")
            _git(origin, "commit", "-qm", "release work")
            release_sha = _git(origin, "rev-parse", "HEAD")
            _git(origin, "checkout", "-qb", "feature")
            _write(origin, "a.txt", "changed\n")
            _git(origin, "add", "-A")
            _git(origin, "commit", "-qm", "feature work")

            clone = os.path.join(tmp, "clone")
            _git(tmp, "clone", "-q", "--branch", "feature", "--single-branch",
                 origin, clone)
            _git(clone, "remote", "set-head", "origin", "-d")

            info = gitio.repo_info(cwd=clone, base_ref="release")
            self.assertNotEqual(release_sha, main_sha)  # guard the fixture
            self.assertEqual(info.base_sha, release_sha)
            self.assertEqual(info.base_branch, "release")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_explicit_base_resolves_via_origin_on_an_ordinary_clone(self) -> None:
        """`--base release/3.0` when the branch exists ONLY as
        refs/remotes/origin/release/3.0 — the normal state of a release branch
        never checked out locally, and an ordinary clone, not the fetch path.

        base_ref used to contribute only its bare spelling while every other
        candidate contributed both, so this candidate failed, `origin/main`
        resolved next, and the override was silently dropped for the default
        branch.
        """
        tmp = tempfile.mkdtemp(prefix="crux-baseremote-")
        try:
            origin = os.path.join(tmp, "origin")
            os.makedirs(origin)
            _git(origin, "init", "-q", "-b", "main")
            _git(origin, "config", "user.email", "t@example.com")
            _git(origin, "config", "user.name", "Test")
            _write(origin, "a.txt", "base\n")
            _git(origin, "add", "-A")
            _git(origin, "commit", "-qm", "base")
            main_sha = _git(origin, "rev-parse", "HEAD")
            _git(origin, "checkout", "-qb", "release/3.0")
            _write(origin, "a.txt", "release\n")
            _git(origin, "add", "-A")
            _git(origin, "commit", "-qm", "release work")
            release_sha = _git(origin, "rev-parse", "HEAD")
            _git(origin, "checkout", "-qb", "feature")
            _write(origin, "a.txt", "changed\n")
            _git(origin, "add", "-A")
            _git(origin, "commit", "-qm", "feature work")
            _git(origin, "checkout", "-q", "main")

            clone = os.path.join(tmp, "clone")
            # A FULL clone: every branch is present as origin/<name>, and only
            # the default branch is checked out locally. No fetch path here.
            _git(tmp, "clone", "-q", origin, clone)
            _git(clone, "checkout", "-q", "-b", "feature", "origin/feature")
            self.assertIsNone(  # guard: no LOCAL release/3.0 ref exists
                gitio._try_git(["show-ref", "--verify", "--quiet",
                                "refs/heads/release/3.0"], clone))

            info = gitio.repo_info(cwd=clone, base_ref="release/3.0")
            self.assertNotEqual(release_sha, main_sha)  # guard the fixture
            self.assertEqual(info.base_sha, release_sha)
            self.assertEqual(info.base_branch, "release/3.0")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _clone_of_a_remote_branch(self, tmp: str,
                                  local: str) -> tuple[str, str]:
        """An ordinary full clone whose working branch was made the ordinary
        way: `git checkout -b <local> origin/feature`. (clone path, main sha).
        """
        origin = os.path.join(tmp, "origin")
        os.makedirs(origin)
        _git(origin, "init", "-q", "-b", "main")
        _git(origin, "config", "user.email", "t@example.com")
        _git(origin, "config", "user.name", "Test")
        _write(origin, "a.txt", "base\n")
        _git(origin, "add", "-A")
        _git(origin, "commit", "-qm", "base")
        main_sha = _git(origin, "rev-parse", "HEAD")
        _git(origin, "checkout", "-qb", "feature")
        _write(origin, "a.txt", "changed\n")
        _git(origin, "add", "-A")
        _git(origin, "commit", "-qm", "feature work")
        _git(origin, "checkout", "-q", "main")

        clone = os.path.join(tmp, "clone")
        _git(tmp, "clone", "-q", origin, clone)
        _git(clone, "checkout", "-q", "-b", local, "origin/feature")
        return clone, main_sha

    def test_branch_made_from_its_own_upstream(self) -> None:
        """`git checkout -b feature origin/feature`, the ordinary way to pick
        up a branch that already exists on the remote.

        git writes "branch: Created from origin/feature", which names THIS
        branch, so the recorded parent was `feature` itself and the merge-base
        of feature and origin/feature is HEAD: `crux preview` produced a
        zero-line diff, with no error and no warning. The base is the
        fork point from main, and no parent is recorded at all.
        """
        tmp = tempfile.mkdtemp(prefix="crux-ownupstream-")
        try:
            clone, main_sha = self._clone_of_a_remote_branch(tmp, "feature")
            info = gitio.repo_info(cwd=clone)
            self.assertIsNone(info.crux_base)  # nothing is its own parent
            self.assertEqual(info.base_sha, main_sha)
            self.assertEqual(info.base_branch, "main")
            self.assertEqual(sorted({h.file for h in gitio.diff_hunks(info)}),
                             ["a.txt"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_a_base_that_already_contains_head_is_not_a_base(self) -> None:
        """The same clone with the local branch under a different name:
        `checkout -b review-feature origin/feature`.

        `feature` is a truthful parent here — it is not this branch — but it
        already contains every commit under review, so its merge-base is HEAD
        and the diff against it is empty. The search has to go on to main
        instead of stopping on a base that resolves to HEAD.
        """
        tmp = tempfile.mkdtemp(prefix="crux-headbase-")
        try:
            clone, main_sha = self._clone_of_a_remote_branch(
                tmp, "review-feature")
            info = gitio.repo_info(cwd=clone)
            self.assertEqual(info.crux_base, "feature")  # where it came from
            self.assertEqual(info.base_sha, main_sha)
            self.assertEqual(info.base_branch, "main")
            self.assertEqual(sorted({h.file for h in gitio.diff_hunks(info)}),
                             ["a.txt"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_an_empty_review_warns_and_stays_off_the_network(self) -> None:
        """A branch carrying nothing of its own: every candidate is either
        absent or already contains HEAD.

        The empty diff is the right answer there, but silence is not — it
        reads exactly like "nothing to flag". And the answer is local: a ref
        that contains HEAD is not turned into a base by fetching it, and
        repo_info runs inside git hooks, which must not pay for the network to
        learn that.
        """
        tmp = tempfile.mkdtemp(prefix="crux-emptybase-")
        try:
            repo = os.path.join(tmp, "r")
            os.makedirs(repo)
            _git(repo, "init", "-q", "-b", "main")
            _git(repo, "config", "user.email", "t@example.com")
            _git(repo, "config", "user.name", "Test")
            _write(repo, "a.txt", "hello\n")
            _git(repo, "add", "-A")
            _git(repo, "commit", "-qm", "only")
            _git(repo, "checkout", "-qb", "feature")  # no commits of its own

            calls: list[list[str]] = []
            real_run = gitio.run_git

            def spy(args, cwd=None, env=None, timeout=None):
                calls.append(args)
                return real_run(args, cwd=cwd, env=env, timeout=timeout)

            with mock.patch("crux.gitio.run_git", side_effect=spy):
                with self.assertLogs("crux.gitio", level="WARNING") as caught:
                    info = gitio.repo_info(cwd=repo)
            self.assertEqual(info.base_sha, info.head_sha)
            self.assertIn("the diff will be empty", "\n".join(caught.output))
            self.assertEqual([c for c in calls if c[:1] == ["fetch"]], [])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_fetch_candidates_are_unprefixed_and_deduped(self) -> None:
        """_fetch_base_sha's candidate list, pinned directly.

        Two things it must do and neither shows up in an end-to-end assertion:
        strip `origin/` (``git fetch origin origin/main`` is not a refspec git
        accepts, so the override would be dropped again), and drop duplicates
        (base_ref == crux_base must not buy a second network round trip on
        the slow path).
        """
        tried: list[str] = []

        def fake_run_git(args, cwd=None, timeout=None):
            tried.append(args[-1])
            raise gitio.GitError("no such ref")

        with mock.patch.object(gitio, "run_git", fake_run_git):
            self.assertIsNone(gitio._fetch_base_sha(
                "/repo", "origin/release", "release", "main"))
        self.assertEqual(tried, ["release", "main"])

    def test_no_origin_repo(self) -> None:
        tmp = tempfile.mkdtemp(prefix="crux-noorigin-")
        try:
            repo = os.path.join(tmp, "r")
            os.makedirs(repo)
            _git(repo, "init", "-q", "-b", "main")
            _git(repo, "config", "user.email", "t@example.com")
            _git(repo, "config", "user.name", "Test")
            _write(repo, "a.txt", "hello\n")
            _git(repo, "add", "-A")
            _git(repo, "commit", "-qm", "only")
            info = gitio.repo_info(cwd=repo)
            self.assertEqual((info.owner, info.repo), ("", ""))
            self.assertEqual(info.default_branch, "main")
            self.assertEqual(info.base_sha, info.head_sha)  # on main itself
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class StackedRepoFixture(unittest.TestCase):
    """A stacked topology: main -> parent -> child, plus origin/* refs.

    This is the shape D34 is about — a branch whose PR does NOT target main.
    """

    def setUp(self) -> None:
        env = mock.patch.dict(os.environ, {
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
        })
        env.start()
        self.addCleanup(env.stop)
        tmp = tempfile.mkdtemp(prefix="crux-stacked-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        self.repo = os.path.join(tmp, "r")
        os.makedirs(self.repo)
        _git(self.repo, "init", "-q", "-b", "main")
        _git(self.repo, "config", "user.email", "t@example.com")
        _git(self.repo, "config", "user.name", "Test")
        _git(self.repo, "remote", "add", "origin",
             "git@github.com:example-org/widget.git")
        _write(self.repo, "a.py", "x = 1\n")
        _git(self.repo, "add", "-A"); _git(self.repo, "commit", "-qm", "on main")
        self.main_sha = _git(self.repo, "rev-parse", "HEAD")

        _git(self.repo, "checkout", "-qb", "parent")
        _write(self.repo, "b.py", "y = 2\n")
        _git(self.repo, "add", "-A"); _git(self.repo, "commit", "-qm", "on parent")
        self.parent_sha = _git(self.repo, "rev-parse", "HEAD")

        # Back to main, THEN create the child off parent: HEAD moves, so @{-1}
        # says "main" and only the reflog knows the real start point.
        _git(self.repo, "checkout", "-q", "main")
        _git(self.repo, "switch", "-qc", "child", "parent")
        _write(self.repo, "c.py", "z = 3\n")
        _git(self.repo, "add", "-A"); _git(self.repo, "commit", "-qm", "on child")
        self.child_sha = _git(self.repo, "rev-parse", "HEAD")
        for branch, sha in (("main", self.main_sha), ("parent", self.parent_sha)):
            _git(self.repo, "update-ref", f"refs/remotes/origin/{branch}", sha)


class TestBranchStartPoint(StackedRepoFixture):
    """D34: git's own reflog names the real start point of a branch."""

    def test_local_branch_start_point(self) -> None:
        self.assertEqual(gitio.branch_start_point("child", self.repo), "parent")

    def test_remote_tracking_start_point_is_reported_bare(self) -> None:
        _git(self.repo, "switch", "-qc", "fromremote", "origin/parent")
        self.assertEqual(gitio.branch_start_point("fromremote", self.repo),
                         "parent")

    def test_created_from_head_is_left_to_the_caller(self) -> None:
        # `checkout -b` off the current branch records "Created from HEAD",
        # which names no branch — the post-checkout hook resolves it via @{-1}.
        _git(self.repo, "checkout", "-qb", "plain")
        self.assertIsNone(gitio.branch_start_point("plain", self.repo))

    def test_start_point_that_is_not_a_branch(self) -> None:
        _git(self.repo, "switch", "-qc", "fromsha", self.main_sha)
        self.assertIsNone(gitio.branch_start_point("fromsha", self.repo))

    def test_deleted_start_point_branch(self) -> None:
        _git(self.repo, "switch", "-qc", "temp", "main")
        _git(self.repo, "switch", "-qc", "offtemp", "temp")
        _git(self.repo, "checkout", "-q", "main")
        _git(self.repo, "branch", "-qD", "temp")
        self.assertIsNone(gitio.branch_start_point("offtemp", self.repo))

    def test_unknown_branch_is_none(self) -> None:
        self.assertIsNone(gitio.branch_start_point("no-such-branch", self.repo))

    def test_fresh_only_rejects_a_branch_with_later_entries(self) -> None:
        # child has a commit on it, so it is no longer freshly created: the
        # post-checkout hook must not mistake a plain switch for a creation.
        self.assertIsNone(
            gitio.branch_start_point("child", self.repo, fresh_only=True))
        _git(self.repo, "switch", "-qc", "brandnew", "parent")
        self.assertEqual(
            gitio.branch_start_point("brandnew", self.repo, fresh_only=True),
            "parent")


class TestRepoInfoBase(StackedRepoFixture):
    def test_reflog_start_point_is_the_base_without_cruxbase(self) -> None:
        # No branch.child.cruxBase is set (the hook never ran), yet the diff
        # base must still be the parent fork point, not main.
        _git(self.repo, "checkout", "-q", "child")
        info = gitio.repo_info(cwd=self.repo)
        self.assertEqual(info.crux_base, "parent")
        self.assertEqual(info.base_sha, self.parent_sha)
        self.assertEqual(info.base_branch, "parent")
        self.assertEqual(sorted({h.file for h in gitio.diff_hunks(info)}),
                         ["c.py"])  # main-based would also show b.py

    def test_base_branch_names_the_winning_candidate(self) -> None:
        _git(self.repo, "checkout", "-q", "main")
        info = gitio.repo_info(cwd=self.repo)
        self.assertEqual(info.base_branch, "main")

    def test_explicit_base_ref_wins_and_is_named(self) -> None:
        _git(self.repo, "checkout", "-q", "child")
        info = gitio.repo_info(cwd=self.repo, base_ref="main")
        self.assertEqual(info.base_sha, self.main_sha)
        self.assertEqual(info.base_branch, "main")


class TestWithBase(StackedRepoFixture):
    """D34: the PR's own base overrides whatever was guessed locally."""

    def setUp(self) -> None:
        super().setUp()
        _git(self.repo, "checkout", "-q", "child")
        self.info = gitio.repo_info(cwd=self.repo)

    def test_realigns_the_diff_onto_the_prs_base(self) -> None:
        # The PR was retargeted from parent to main: the review must follow.
        aligned = gitio.with_base(self.info, "main")
        self.assertEqual(aligned.base_sha, self.main_sha)
        self.assertEqual(aligned.base_branch, "main")
        self.assertEqual(sorted({h.file for h in gitio.diff_hunks(aligned)}),
                         ["b.py", "c.py"])  # the PR really does show both

    def test_matching_base_is_a_no_op(self) -> None:
        self.assertIs(gitio.with_base(self.info, "parent"), self.info)

    def test_empty_base_is_a_no_op(self) -> None:
        self.assertIs(gitio.with_base(self.info, None), self.info)
        self.assertIs(gitio.with_base(self.info, ""), self.info)

    def test_leaves_crux_base_alone(self) -> None:
        # crux_base records where the branch CAME FROM (D17); base_branch says
        # what the diff is against. Aligning must not rewrite history.
        aligned = gitio.with_base(self.info, "main")
        self.assertEqual(aligned.crux_base, "parent")

    def _spy_run_git(self, calls: list[list[str]], on_fetch):
        """Patch run_git so the one network call is simulated, never made."""
        real_run = gitio.run_git

        def spy(args, cwd=None, env=None, timeout=None):
            calls.append(args)
            if args[:1] == ["fetch"]:
                return on_fetch(cwd)
            return real_run(args, cwd=cwd, env=env, timeout=timeout)

        return mock.patch("crux.gitio.run_git", side_effect=spy)

    def test_unresolvable_base_keeps_the_local_guess(self) -> None:
        # The fetch fails (no such branch on the remote) and nothing resolves:
        # the run keeps its guess instead of dying or diffing against nothing.
        def failing_fetch(cwd):
            raise gitio.GitError("couldn't find remote ref")

        with self._spy_run_git([], failing_fetch):
            aligned = gitio.with_base(self.info, "no-such-branch-anywhere")
        self.assertEqual(aligned.base_sha, self.info.base_sha)
        self.assertEqual(aligned.base_branch, "parent")

    def test_fetches_a_base_this_clone_has_never_seen(self) -> None:
        # A base known only to the remote: with_base fetches it once and falls
        # back to FETCH_HEAD when the fetch leaves no local ref behind.
        calls: list[list[str]] = []

        def landing_fetch(cwd):
            path = os.path.join(cwd, ".git", "FETCH_HEAD")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(f"{self.main_sha}\t\tbranch 'never-fetched' of origin\n")
            return ""

        with self._spy_run_git(calls, landing_fetch):
            aligned = gitio.with_base(self.info, "never-fetched")
        self.assertEqual(aligned.base_sha, self.main_sha)
        self.assertEqual(aligned.base_branch, "never-fetched")
        fetch = next(c for c in calls if c[:1] == ["fetch"])
        self.assertEqual(fetch[-2:], ["origin", "never-fetched"])

    def test_the_fetch_is_bounded_by_a_timeout(self) -> None:
        # A stalled remote must never hang a review: the only network call
        # carries a timeout, and a timeout leaves the local guess in place.
        seen: dict = {}
        real_run = subprocess.run

        def spy(argv, **kwargs):
            if argv[:2] == ["git", "fetch"]:
                seen["timeout"] = kwargs.get("timeout")
                raise subprocess.TimeoutExpired(cmd="git",
                                                timeout=gitio._FETCH_TIMEOUT)
            return real_run(argv, **kwargs)

        with mock.patch("crux.gitio.subprocess.run", side_effect=spy):
            aligned = gitio.with_base(self.info, "never-fetched")
        self.assertEqual(seen.get("timeout"), gitio._FETCH_TIMEOUT)
        self.assertEqual(aligned.base_sha, self.info.base_sha)

    def test_run_git_timeout_maps_to_giterror(self) -> None:
        exc = subprocess.TimeoutExpired(cmd="git", timeout=1)
        with mock.patch("crux.gitio.subprocess.run", side_effect=exc):
            with self.assertRaises(gitio.GitError) as ctx:
                gitio.run_git(["fetch"], cwd=self.repo, timeout=1)
        self.assertIn("timed out", str(ctx.exception))

    def test_run_git_timeout_maps_to_giterror(self) -> None:
        exc = subprocess.TimeoutExpired(cmd="git", timeout=1)
        with mock.patch("crux.gitio.subprocess.run", side_effect=exc):
            with self.assertRaises(gitio.GitError) as ctx:
                gitio.run_git(["fetch"], cwd=self.repo, timeout=1)
        self.assertIn("timed out", str(ctx.exception))


class TestDiffHunks(RepoFixture):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.info = gitio.repo_info(cwd=cls.repo)
        cls.hunks = gitio.diff_hunks(cls.info)
        cls.by_file: dict[str, list[Hunk]] = {}
        for h in cls.hunks:
            cls.by_file.setdefault(h.file, []).append(h)

    def test_files_present_and_binary_skipped(self) -> None:
        files = set(self.by_file)
        self.assertEqual(
            files, {"lib.py", "deep.py", "gone.py", "newfile.py", "newname.py"})
        self.assertNotIn("blob.bin", files)
        self.assertNotIn("oldname.py", files)  # rename uses the new path

    def test_multiple_hunks_per_file(self) -> None:
        lib = self.by_file["lib.py"]
        self.assertEqual(len(lib), 2)
        first, second = lib
        self.assertLess(first.new_start, second.new_start)
        self.assertIn("    b = 20", first.added_lines)
        self.assertIn("    b = 2", first.removed_lines)
        self.assertIn("    return value + extra + 1", second.added_lines)
        self.assertEqual(first.enclosing_symbol, "alpha")
        self.assertEqual(second.enclosing_symbol, "omega")

    def test_hunk_id_format(self) -> None:
        for h in self.hunks:
            self.assertEqual(h.id, f"{h.file}:{h.new_start}")

    def test_new_file(self) -> None:
        (h,) = self.by_file["newfile.py"]
        self.assertEqual((h.old_start, h.old_count), (0, 0))
        self.assertEqual(h.new_start, 1)
        self.assertEqual(h.id, "newfile.py:1")
        self.assertEqual(h.added_lines, ["def fresh():", "    return 42"])
        self.assertEqual(h.enclosing_symbol, "fresh")

    def test_deleted_file(self) -> None:
        (h,) = self.by_file["gone.py"]
        self.assertEqual((h.new_start, h.new_count), (0, 0))
        self.assertEqual(h.id, "gone.py:0")
        self.assertIn("def gone():", h.removed_lines)
        self.assertEqual(h.enclosing_symbol, "gone")

    def test_rename_uses_new_path(self) -> None:
        (h,) = self.by_file["newname.py"]
        self.assertIn("    d = 44", h.added_lines)
        self.assertEqual(h.enclosing_symbol, "renamed_helper")

    def test_enclosing_symbol_outside_context_window(self) -> None:
        # e = 5 is 5 lines below "def deep():", so the def is not in the
        # -U3 context lines; the @@ trailer or head-file scan must find it.
        (h,) = self.by_file["deep.py"]
        self.assertIn("    e = 50", h.added_lines)
        self.assertEqual(h.enclosing_symbol, "deep")

    def test_patch_keeps_context_and_header(self) -> None:
        (h,) = self.by_file["deep.py"]
        lines = h.patch.split("\n")
        self.assertTrue(lines[0].startswith("@@ "))
        self.assertIn("     d = 4", lines)  # context line kept, prefix intact

    def test_empty_diff(self) -> None:
        info = RepoInfo(root=self.repo, branch="feature",
                        head_sha=self.head_sha, base_sha=self.head_sha,
                        owner="o", repo="r")
        self.assertEqual(gitio.diff_hunks(info), [])


class TestEnclosingSymbolPaths(RepoFixture):
    """Exercise the trailer and head-file-scan fallbacks deterministically."""

    def _info(self) -> RepoInfo:
        return gitio.repo_info(cwd=self.repo)

    def test_trailer_fallback(self) -> None:
        hunk = Hunk(id="scanner.py:7", file="scanner.py",
                    old_start=7, old_count=2, new_start=7, new_count=2,
                    patch="@@ -7,2 +7,2 @@ def trailer_func(x):\n"
                          "     y = 2\n-    z = 3\n+    z = 30")
        sym = gitio._enclosing_symbol(hunk, self._info(), {})
        self.assertEqual(sym, "trailer_func")

    def test_head_file_scan_fallback(self) -> None:
        # No symbol in the body and no @@ trailer: must read scanner.py at
        # head and scan upward from line 8 to find "hidden" (line 5).
        hunk = Hunk(id="scanner.py:8", file="scanner.py",
                    old_start=8, old_count=2, new_start=8, new_count=2,
                    patch="@@ -8,2 +8,2 @@\n     z = 3\n-    w = 4\n+    w = 40")
        sym = gitio._enclosing_symbol(hunk, self._info(), {})
        self.assertEqual(sym, "hidden")

    def test_deleted_file_never_scans_head(self) -> None:
        hunk = Hunk(id="whatever.py:0", file="whatever.py",
                    old_start=1, old_count=2, new_start=0, new_count=0,
                    patch="@@ -1,2 +0,0 @@\n-    x = 1\n-    y = 2")
        sym = gitio._enclosing_symbol(hunk, self._info(), {})
        self.assertIsNone(sym)


class TestParseDiffSpacePaths(unittest.TestCase):
    """Git appends a literal TAB to '---'/'+++' paths containing spaces (for
    quoted paths the tab lands OUTSIDE the closing quote). The parser must
    strip it or every downstream lookup/link uses a wrong path."""

    def test_unquoted_path_with_spaces_strips_trailing_tab(self) -> None:
        diff = (
            "diff --git a/file with spaces.py b/file with spaces.py\n"
            "index 0000000..1111111 100644\n"
            "--- a/file with spaces.py\t\n"
            "+++ b/file with spaces.py\t\n"
            "@@ -1,1 +1,1 @@\n"
            "-old\n"
            "+new\n"
        )
        (h,) = gitio._parse_diff(diff)
        self.assertEqual(h.file, "file with spaces.py")
        self.assertEqual(h.id, "file with spaces.py:1")

    def test_quoted_unicode_path_with_spaces(self) -> None:
        diff = (
            'diff --git "a/spac\\303\\251 name.txt" "b/spac\\303\\251 name.txt"\n'
            "index 0000000..1111111 100644\n"
            '--- "a/spac\\303\\251 name.txt"\t\n'
            '+++ "b/spac\\303\\251 name.txt"\t\n'
            "@@ -1,1 +1,1 @@\n"
            "-old\n"
            "+new\n"
        )
        (h,) = gitio._parse_diff(diff)
        self.assertEqual(h.file, "spacé name.txt")
        self.assertEqual(h.id, "spacé name.txt:1")


class TestSymbolRegex(unittest.TestCase):
    def test_matches(self) -> None:
        cases = [
            ("def alpha():", "alpha"),
            ("    async def handler(x):", "handler"),
            ("class Foo(Base):", "Foo"),
            ("function doThing(a) {", "doThing"),
            ("export default function App() {", "App"),
            ("fn main() {", "main"),
            ("pub fn parse(input: &str) {", "parse"),
            ("pub(crate) async fn run() {", "run"),
            ("func Handle(w, r) {", "Handle"),
            ("func (s *Server) Serve(l net.Listener) error {", "Serve"),
            ("impl Display for Point {", "Display"),
            ("impl<T: Clone> Stack<T> {", "Stack"),
            ("def __init__(self):", "__init__"),
        ]
        for line, expected in cases:
            with self.subTest(line=line):
                self.assertEqual(gitio._symbol_from_line(line), expected)

    def test_non_matches(self) -> None:
        for line in ["define x", "    return func_result", "functional test",
                     "x = 1", "# def commented(a):",
                     "CONSTANT = 1", "@decorator", ""]:
            with self.subTest(line=line):
                self.assertIsNone(gitio._symbol_from_line(line))


if __name__ == "__main__":
    unittest.main()
