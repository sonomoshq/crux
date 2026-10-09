# Copyright © 2026 Sonomos, Inc.
# SPDX-License-Identifier: Apache-2.0

"""Tests for crux.commitmsg (D27 commit-message enrichment, D28 pre-push net).

The D27 tests fake git and claude at the module seams
(`crux.commitmsg.run_git`, `crux.commitmsg.claude_json`); no subprocesses
run. The D28 `ensure_enriched` tests use REAL git in a temp repo — the
commit-tree/update-ref plumbing is exactly what needs proving — with only
`claude_json` faked.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import crux.commitmsg as commitmsg
from crux.commitmsg import (
    COMMIT_TRAILER,
    ENRICH_PREFIX,
    build_message,
    build_prompt,
    effective_subject,
    enrich,
    ensure_enriched,
)
from crux.gitio import GitError
from crux.models import Config, LlmError, RepoInfo

SHA = "a" * 40

CLAUDE_MSG = ("Add write buffer\n\nDetails here.\n\n"
              "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>\n")


class FakeGit:
    """Answers the exact git calls commitmsg makes; records amends."""

    def __init__(self, root: str, message: str = "fix") -> None:
        self.root = root
        self.head = SHA
        self.message = message
        self.parents = 1
        self.staged_clean = True
        self.remote_refs = ""       # `branch -r --contains` output
        self.diff = "diff --git a/x.py b/x.py\n+x = 1\n"
        self.amends: list[str] = []

    def __call__(self, args: list[str], cwd: str | None = None) -> str:
        if args == ["rev-parse", "HEAD"]:
            return self.head
        if args[:3] == ["log", "-1", "--format=%B"]:
            return self.message
        if args[:2] == ["rev-parse", "--git-path"]:
            return f".git/{args[2]}"  # nothing exists under the tmp root
        if args[:3] == ["diff", "--cached", "--quiet"]:
            if self.staged_clean:
                return ""
            raise GitError("staged changes")
        if args[:3] == ["branch", "-r", "--contains"]:
            return self.remote_refs
        if args[:4] == ["rev-list", "--parents", "-n", "1"]:
            return " ".join([SHA] + ["p" * 40] * self.parents)
        if args[:1] == ["show"]:
            return self.diff
        if args[:2] == ["commit", "--amend"]:
            self.amends.append(args[args.index("-m") + 1])
            return ""
        raise AssertionError(f"unexpected git call: {args}")


class CommitMsgBase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.info = RepoInfo(root=tmp.name, branch="feat/x", head_sha=SHA,
                             base_sha="b" * 40, owner="example-org", repo="demo")
        self.cfg = Config()
        self.git = FakeGit(tmp.name)
        git_patch = mock.patch.object(commitmsg, "run_git", self.git)
        git_patch.start()
        self.addCleanup(git_patch.stop)
        self.llm = mock.patch.object(commitmsg, "claude_json", return_value={
            "subject": "Fix flush losing buffered events on shutdown",
            "bullets": ["Buffered events now drain before the process exits"],
        })
        self.claude = self.llm.start()
        self.addCleanup(self.llm.stop)


class TestGuards(CommitMsgBase):

    def _assert_skipped(self) -> None:
        self.assertIsNone(enrich(self.info, self.cfg, SHA))
        self.assertEqual(self.git.amends, [])

    def test_disabled_by_config(self) -> None:
        self.cfg.commit_enrich = False
        self._assert_skipped()
        self.claude.assert_not_called()

    def test_claude_authored_commit_left_alone(self) -> None:
        self.git.message = CLAUDE_MSG
        self._assert_skipped()
        self.claude.assert_not_called()

    def test_lowercase_co_author_trailer_detected(self) -> None:
        self.git.message = "quick fix\n\nco-authored-by: claude <x@y>\n"
        self._assert_skipped()

    def test_already_amended_commit_left_alone(self) -> None:
        self.git.message = f"fix\n\nCrux: Better subject\n\n{COMMIT_TRAILER}\n"
        self._assert_skipped()
        self.claude.assert_not_called()

    def test_merge_commit_left_alone(self) -> None:
        self.git.parents = 2
        self._assert_skipped()
        self.claude.assert_not_called()

    def test_moved_head_blocks(self) -> None:
        self.git.head = "c" * 40
        self._assert_skipped()
        self.claude.assert_not_called()

    def test_dirty_index_blocks(self) -> None:
        self.git.staged_clean = False
        self._assert_skipped()

    def test_pushed_commit_never_rewritten(self) -> None:
        self.git.remote_refs = "  origin/feat/x"
        self._assert_skipped()

    def test_in_progress_rebase_blocks(self) -> None:
        (Path(self.info.root) / ".git").mkdir()
        (Path(self.info.root) / ".git" / "rebase-merge").mkdir()
        self._assert_skipped()

    def test_guards_recheck_after_the_llm_call(self) -> None:
        # The user commits again while claude is thinking: HEAD moves between
        # the first guard pass and the amend — nothing may be rewritten.
        def move_head(prompt: str, cfg: Config) -> dict:
            self.git.head = "d" * 40
            return {"subject": "Better", "bullets": []}
        self.claude.side_effect = move_head
        self.assertIsNone(enrich(self.info, self.cfg, SHA))
        self.assertEqual(self.git.amends, [])

    def test_empty_llm_subject_skips_the_amend(self) -> None:
        self.claude.side_effect = None
        self.claude.return_value = {"subject": "", "bullets": ["x"]}
        self._assert_skipped()


class TestEnrichHappyPath(CommitMsgBase):

    def test_amends_with_human_message_on_top(self) -> None:
        subject = enrich(self.info, self.cfg, SHA)
        self.assertEqual(subject, "Fix flush losing buffered events on shutdown")
        self.assertEqual(len(self.git.amends), 1)
        message = self.git.amends[0]
        lines = message.splitlines()
        # The human's message stays verbatim as the first line; Crux's part
        # is pasted below; the trailer is the last non-empty line.
        self.assertEqual(lines[0], "fix")
        self.assertIn(f"{ENRICH_PREFIX}Fix flush losing buffered events on shutdown",
                      lines)
        self.assertIn("- Buffered events now drain before the process exits",
                      lines)
        self.assertEqual(message.rstrip().splitlines()[-1], COMMIT_TRAILER)

    def test_amend_uses_no_verify_and_allow_empty(self) -> None:
        recorded: list[list[str]] = []
        original = self.git.__call__

        def spy(args: list[str], cwd: str | None = None) -> str:
            if args[:2] == ["commit", "--amend"]:
                recorded.append(args)
            return original(args, cwd)
        with mock.patch.object(commitmsg, "run_git", spy):
            enrich(self.info, self.cfg, SHA)
        self.assertEqual(len(recorded), 1)
        self.assertIn("--no-verify", recorded[0])
        self.assertIn("--allow-empty", recorded[0])

    def test_amended_message_is_skipped_on_the_next_run(self) -> None:
        # Recursion guard: the amend re-fires post-commit -> enrich again.
        enrich(self.info, self.cfg, SHA)
        self.git.message = self.git.amends[0]
        self.claude.reset_mock()
        self.assertIsNone(enrich(self.info, self.cfg, SHA))
        self.claude.assert_not_called()
        self.assertEqual(len(self.git.amends), 1)

    def test_multiline_human_message_kept_verbatim(self) -> None:
        self.git.message = "fix\n\nI think this handles the timeout case.\n"
        enrich(self.info, self.cfg, SHA)
        self.assertTrue(self.git.amends[0].startswith(
            "fix\n\nI think this handles the timeout case."))


class TestPrompt(CommitMsgBase):

    def test_prompt_carries_branch_original_and_diff(self) -> None:
        # Also a regression test for brace doubling in prompts/commit.md:
        # a stray single brace makes .format raise -> CruxError.
        prompt = build_prompt(self.info, SHA, "fix")
        self.assertIn("feat/x", prompt)
        self.assertIn("fix", prompt)
        self.assertIn("diff --git a/x.py", prompt)
        self.assertIn('"subject"', prompt)

    def test_diff_is_truncated(self) -> None:
        self.git.diff = "\n".join(f"+line {i}" for i in range(1000))
        prompt = build_prompt(self.info, SHA, "fix")
        self.assertIn("truncated", prompt)
        self.assertNotIn("+line 999", prompt)


class TestMessageAssembly(unittest.TestCase):

    def test_build_message_shape(self) -> None:
        msg = build_message("Better subject", ["one", "two"], "fix")
        self.assertEqual(
            msg,
            "fix\n\nCrux: Better subject\n\n- one\n- two\n\n"
            f"{COMMIT_TRAILER}\n")

    def test_build_message_without_bullets(self) -> None:
        msg = build_message("Better subject", [], "fix")
        self.assertEqual(msg, f"fix\n\nCrux: Better subject\n\n{COMMIT_TRAILER}\n")

    def test_coerce_clamps_subject_and_bullets(self) -> None:
        subject, bullets = commitmsg._coerce({
            "subject": "x" * 100 + ".",
            "bullets": [f"- b{i}" for i in range(9)],
        })
        self.assertLessEqual(len(subject), 72)
        self.assertFalse(subject.endswith("."))
        self.assertEqual(len(bullets), 5)
        self.assertEqual(bullets[0], "b0")  # leading dash stripped

    def test_coerce_accepts_a_string_bullet(self) -> None:
        _, bullets = commitmsg._coerce({"subject": "s", "bullets": "just one"})
        self.assertEqual(bullets, ["just one"])


class EnsureEnrichedTests(unittest.TestCase):
    """D28 pre-push safety net, exercised against a real git repo."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name
        self._git("init", "-q", "-b", "main")
        self._git("config", "user.email", "human@example.com")
        self._git("config", "user.name", "Human")
        self._git("config", "commit.gpgsign", "false")
        self.cfg = Config()
        self.llm_calls: list[str] = []

        def fake_llm(prompt: str, cfg: Config) -> dict:
            self.llm_calls.append(prompt)
            return {"subject": f"Expanded subject {len(self.llm_calls)}",
                    "bullets": ["what changed and why"]}

        patcher = mock.patch.object(commitmsg, "claude_json",
                                    side_effect=fake_llm)
        self.claude = patcher.start()
        self.addCleanup(patcher.stop)

    # -- plumbing -----------------------------------------------------------

    def _git(self, *args: str) -> str:
        proc = subprocess.run(["git", *args], cwd=self.root, check=True,
                              capture_output=True, text=True)
        return proc.stdout.strip()

    def _commit(self, name: str, message: str) -> str:
        (Path(self.root) / name).write_text(name, encoding="utf-8")
        self._git("add", name)
        self._git("commit", "-q", "-m", message)
        return self._git("rev-parse", "HEAD")

    def _mark_pushed(self, sha: str) -> None:
        self._git("update-ref", "refs/remotes/origin/main", sha)

    def _info(self) -> RepoInfo:
        head = self._git("rev-parse", "HEAD")
        return RepoInfo(root=self.root, branch="main", head_sha=head,
                        base_sha=head, owner="example-org", repo="demo")

    def _message(self, sha: str = "HEAD") -> str:
        return self._git("log", "-1", "--format=%B", sha)

    # -- nothing to do ------------------------------------------------------

    def test_everything_already_pushed(self) -> None:
        self._mark_pushed(self._commit("a.py", "fix"))
        self.assertFalse(ensure_enriched(self._info(), self.cfg))
        self.claude.assert_not_called()

    def test_claude_commits_pass_untouched(self) -> None:
        self._commit("a.py", CLAUDE_MSG)
        sha = self._commit("b.py", "More work\n\nco-authored-by: Claude <x@y>")
        self.assertFalse(ensure_enriched(self._info(), self.cfg))
        self.claude.assert_not_called()
        self.assertEqual(self._git("rev-parse", "HEAD"), sha)

    def test_disabled_by_config(self) -> None:
        self._commit("a.py", "fix")
        self.cfg.commit_enrich = False
        self.assertFalse(ensure_enriched(self._info(), self.cfg))
        self.claude.assert_not_called()

    def test_backs_off_above_the_commit_cap(self) -> None:
        for i in range(3):
            self._commit(f"f{i}.py", "wip")
        with mock.patch.object(commitmsg, "_ENSURE_MAX", 2):
            self.assertFalse(ensure_enriched(self._info(), self.cfg))
        self.claude.assert_not_called()

    def test_ignores_tags_and_branch_deletions(self) -> None:
        sha = self._commit("a.py", "fix")
        refs = [("refs/tags/v1", sha), ("refs/heads/dead", "0" * 40)]
        self.assertFalse(ensure_enriched(self._info(), self.cfg, refs=refs))
        self.claude.assert_not_called()

    # -- the race itself ----------------------------------------------------

    def test_terse_head_is_enriched_and_the_push_stopped(self) -> None:
        self._mark_pushed(self._commit("a.py", CLAUDE_MSG))
        old = self._commit("b.py", "fix")
        old_tree = self._git("rev-parse", f"{old}^{{tree}}")
        old_author = self._git("log", "-1", "--format=%an %ae %aD", old)

        stale = ensure_enriched(self._info(), self.cfg)

        self.assertTrue(stale)  # shas changed => this push must stop
        new = self._git("rev-parse", "HEAD")
        self.assertNotEqual(new, old)
        message = self._message()
        self.assertTrue(message.startswith("fix\n"))  # human text on top
        self.assertIn(f"{ENRICH_PREFIX}Expanded subject 1", message)
        self.assertIn(COMMIT_TRAILER, message)
        # message-only rewrite: same tree, same author, clean working copy
        self.assertEqual(self._git("rev-parse", "HEAD^{tree}"), old_tree)
        self.assertEqual(self._git("log", "-1", "--format=%an %ae %aD"),
                         old_author)
        self.assertEqual(self._git("status", "--porcelain"), "")

    def test_deep_terse_chain_is_rebuilt_in_order(self) -> None:
        # Rapid commits: the older enrichment was cancelled by the newer
        # commit (HEAD moved), leaving TWO terse commits — only pre-push can
        # still fix the deep one, since plain amend only reaches HEAD.
        self._commit("a.py", "wip")
        self._commit("b.py", CLAUDE_MSG)
        self._commit("c.py", "more wip")

        self.assertTrue(ensure_enriched(self._info(), self.cfg))

        shas = self._git("rev-list", "--reverse", "HEAD").splitlines()
        self.assertEqual(len(shas), 3)  # linear history preserved
        self.assertIn(ENRICH_PREFIX, self._message(shas[0]))
        self.assertTrue(self._message(shas[0]).startswith("wip\n"))
        # the Claude commit's message is untouched (its sha changed only
        # because its parent did)
        self.assertEqual(self._message(shas[1]).strip(), CLAUDE_MSG.strip())
        self.assertIn(ENRICH_PREFIX, self._message(shas[2]))
        self.assertEqual(len(self.llm_calls), 2)

    def test_second_push_finds_nothing_to_do(self) -> None:
        self._commit("a.py", "fix")
        self.assertTrue(ensure_enriched(self._info(), self.cfg))
        self.claude.reset_mock()
        # the retry push: everything now carries the trailer
        self.assertFalse(ensure_enriched(self._info(), self.cfg))
        self.claude.assert_not_called()

    def test_stale_pushed_sha_detected_without_llm_work(self) -> None:
        # A detached enrich-commit amended HEAD after git resolved the push:
        # the ref no longer matches what git wants to send. No LLM needed —
        # the push is stale on arrival.
        old = self._commit("a.py", "fix")
        self._git("commit", "-q", "--amend", "-m",
                  f"fix\n\nCrux: Better\n\n{COMMIT_TRAILER}")
        refs = [("refs/heads/main", old)]
        self.assertTrue(ensure_enriched(self._info(), self.cfg, refs=refs))
        self.claude.assert_not_called()

    def test_llm_failure_never_stops_the_push(self) -> None:
        self._commit("a.py", "fix")
        self.claude.side_effect = LlmError("claude broke")
        self.assertFalse(ensure_enriched(self._info(), self.cfg))
        self.assertEqual(self._message().strip(), "fix")  # pushed terse

    def test_notify_fires_only_when_there_is_work(self) -> None:
        notices: list[str] = []
        self._mark_pushed(self._commit("a.py", CLAUDE_MSG))
        ensure_enriched(self._info(), self.cfg, notify=notices.append)
        self.assertEqual(notices, [])
        self._commit("b.py", "fix")
        ensure_enriched(self._info(), self.cfg, notify=notices.append)
        self.assertEqual(len(notices), 1)
        self.assertIn("1 commit message", notices[0])


class TestEffectiveSubject(unittest.TestCase):

    def test_plain_message_first_line(self) -> None:
        self.assertEqual(effective_subject("fix\n\nmore text"), "fix")

    def test_amended_message_prefers_the_crux_subject(self) -> None:
        msg = build_message("Fix flush on shutdown", ["a"], "fix")
        self.assertEqual(effective_subject(msg), "Fix flush on shutdown")

    def test_crux_line_without_trailer_is_not_special(self) -> None:
        # Someone typing "Crux: ..." themselves is not an amended commit.
        self.assertEqual(effective_subject("fix\n\nCrux: not really"), "fix")

    def test_empty_message(self) -> None:
        self.assertEqual(effective_subject("  \n"), "")


if __name__ == "__main__":
    unittest.main()
