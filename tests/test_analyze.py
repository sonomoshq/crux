# Copyright © 2026 Sonomos, Inc.
# SPDX-License-Identifier: Apache-2.0

"""Tests for crux.llm and crux.analyze. All subprocess use is mocked: no
network, no gh auth, no real claude calls."""
from __future__ import annotations

import copy
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from crux import analyze, llm
from crux.models import (
    Annotation,
    Badge,
    ChangeMap,
    Config,
    DagEdge,
    DagNode,
    DesignFinding,
    Hunk,
    HunkSignals,
    LlmError,
    MapArrow,
    MapStep,
    NodeAnnotation,
    NotLoggedInError,
    PostError,
    RepoInfo,
    RunState,
    Verdict,
)


def envelope(result: str) -> str:
    """Canned claude CLI JSON envelope whose 'result' field is *result*."""
    return json.dumps({"type": "result", "is_error": False, "result": result})


def fake_run(stdouts: list[str]):
    """subprocess.run replacement returning successive canned stdouts.

    Returns (callable, calls) where calls collects (argv, kwargs) per call.
    """
    calls: list[tuple[list[str], dict]] = []

    def run(argv, **kwargs):
        calls.append((list(argv), kwargs))
        stdout = stdouts[min(len(calls) - 1, len(stdouts) - 1)]
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    return run, calls


GOOD_INNER = {
    "summary": "Adds a write buffer in front of the recipe index.",
    "claims": [
        {"id": "C1", "text": "no events are lost during flush",
         "hunk_ids": ["a.py:10"], "kind": "behavior", "uncertain": True},
    ],
    "nodes": {
        "1": {"title": "flush every 5s means a crash loses at most 5s",
              "why": "blast: 12 call sites depend on add().",
              "questions": ["Is add() safe under concurrent writers?"],
              "chips": ["blast: 12 call sites"],
              "minutes": 99,          # must clamp to 10
              "design_decision": True},
        "7": {"title": "ghost node", "why": "should be dropped"},  # unknown number
    },
    "audit": [
        {"claim": "no events are lost during flush",
         "verdict": "CONTRADICTED",
         "evidence": "a.py:61 drops the batch on flush failure"},
    ],
}


class ExtractJsonObjectTest(unittest.TestCase):
    def test_plain_object(self):
        self.assertEqual(llm.extract_json_object('{"a": 1}'), '{"a": 1}')

    def test_prose_around_and_nested(self):
        text = 'Sure! Here you go:\n{"a": {"b": [1, 2]}, "c": "x"}\nHope that helps.'
        self.assertEqual(json.loads(llm.extract_json_object(text)), {"a": {"b": [1, 2]}, "c": "x"})

    def test_braces_inside_strings(self):
        text = '{"a": "closing } and opening { inside", "b": 2}'
        self.assertEqual(json.loads(llm.extract_json_object(text))["b"], 2)

    def test_skips_non_json_brace_blocks(self):
        text = 'in {file} we set {"a": 1}'
        self.assertEqual(llm.extract_json_object(text), '{"a": 1}')

    def test_no_object(self):
        self.assertIsNone(llm.extract_json_object("no json here"))
        self.assertIsNone(llm.extract_json_object('{"unterminated": '))


class ClaudeJsonTest(unittest.TestCase):
    def setUp(self):
        self.cfg = Config(llm_model="sonnet", llm_timeout=123)
        patcher = mock.patch("crux.llm.shutil.which", return_value="/usr/bin/claude")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_happy_path_argv_and_input(self):
        run, calls = fake_run([envelope('prefix {"ok": true} suffix')])
        with mock.patch("crux.llm.subprocess.run", side_effect=run):
            out = llm.claude_json("PROMPT", self.cfg)
        self.assertEqual(out, {"ok": True})
        self.assertEqual(len(calls), 1)
        argv, kwargs = calls[0]
        self.assertEqual(
            argv, ["/usr/bin/claude", "-p", "--output-format", "json", "--model", "sonnet"])
        self.assertEqual(kwargs["input"], "PROMPT")
        # UTF-8 pinned (not locale-encoded) so claude's output decodes the same
        # on Windows cp1252 as on a UTF-8 POSIX locale.
        self.assertEqual(kwargs["encoding"], "utf-8")
        self.assertEqual(kwargs["errors"], "replace")
        self.assertEqual(kwargs["timeout"], 123)  # cfg.llm_timeout by default

    def test_explicit_timeout_wins(self):
        run, calls = fake_run([envelope('{"ok": 1}')])
        with mock.patch("crux.llm.subprocess.run", side_effect=run):
            llm.claude_json("P", self.cfg, timeout=7)
        self.assertEqual(calls[0][1]["timeout"], 7)

    def test_malformed_then_valid_retries_once(self):
        run, calls = fake_run([
            envelope("Sure, here is my analysis in prose form."),  # no JSON object
            envelope('{"ok": 2}'),
        ])
        with mock.patch("crux.llm.subprocess.run", side_effect=run):
            out = llm.claude_json("PROMPT", self.cfg)
        self.assertEqual(out, {"ok": 2})
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][1]["input"], "PROMPT")
        self.assertTrue(calls[1][1]["input"].endswith("Return ONLY the JSON object."))

    def test_bad_envelope_then_valid(self):
        run, calls = fake_run(["not json at all", envelope('{"ok": 3}')])
        with mock.patch("crux.llm.subprocess.run", side_effect=run):
            self.assertEqual(llm.claude_json("P", self.cfg), {"ok": 3})
        self.assertEqual(len(calls), 2)

    def test_raises_after_two_parse_failures(self):
        run, calls = fake_run([envelope("nope"), envelope("still nope")])
        with mock.patch("crux.llm.subprocess.run", side_effect=run):
            with self.assertRaises(LlmError):
                llm.claude_json("P", self.cfg)
        self.assertEqual(len(calls), 2)

    def test_missing_binary(self):
        with mock.patch("crux.llm.shutil.which", return_value=None):
            with mock.patch("crux.llm.subprocess.run") as run:
                with self.assertRaises(LlmError):
                    llm.claude_json("P", self.cfg)
                run.assert_not_called()

    def test_nonzero_exit_raises_without_retry(self):
        proc = SimpleNamespace(returncode=1, stdout="", stderr="boom")
        with mock.patch("crux.llm.subprocess.run", return_value=proc) as run:
            with self.assertRaises(LlmError) as ctx:
                llm.claude_json("P", self.cfg)
        self.assertEqual(run.call_count, 1)
        self.assertIn("boom", str(ctx.exception))

    def test_nonzero_exit_uses_stdout_when_stderr_empty(self):
        # A real crash sometimes writes only to stdout — surface it, don't
        # report an empty "claude exited 1: ".
        proc = SimpleNamespace(returncode=2, stdout="segfault detail", stderr="")
        with mock.patch("crux.llm.subprocess.run", return_value=proc):
            with self.assertRaises(LlmError) as ctx:
                llm.claude_json("P", self.cfg)
        self.assertIn("segfault detail", str(ctx.exception))
        self.assertNotIsInstance(ctx.exception, NotLoggedInError)

    def test_empty_nonzero_exit_is_not_logged_in(self):
        # Exit nonzero with NOTHING on either stream is the signature of an
        # unauthenticated CLI — the bug that was failing quietly.
        proc = SimpleNamespace(returncode=1, stdout="", stderr="")
        with mock.patch("crux.llm.subprocess.run", return_value=proc) as run:
            with self.assertRaises(NotLoggedInError) as ctx:
                llm.claude_json("P", self.cfg)
        self.assertEqual(run.call_count, 1)  # no retry
        self.assertIn("claude login", str(ctx.exception))
        self.assertIsInstance(ctx.exception, LlmError)  # existing handlers catch it

    def test_auth_message_in_stdout_is_not_logged_in(self):
        proc = SimpleNamespace(
            returncode=1, stdout="Invalid API key · Please run /login", stderr="")
        with mock.patch("crux.llm.subprocess.run", return_value=proc):
            with self.assertRaises(NotLoggedInError):
                llm.claude_json("P", self.cfg)

    def test_timeout_raises(self):
        exc = subprocess.TimeoutExpired(cmd="claude", timeout=123)
        with mock.patch("crux.llm.subprocess.run", side_effect=exc):
            with self.assertRaises(LlmError):
                llm.claude_json("P", self.cfg)


def mk_hunk(hid: str = "a.py:10", file: str = "a.py", patch: str | None = None) -> Hunk:
    return Hunk(
        id=hid, file=file, old_start=10, old_count=3, new_start=10, new_count=5,
        patch=patch or "@@ -10,3 +10,5 @@\n context\n+def add(x):\n+    return x + 1\n context2",
        enclosing_symbol="add",
    )


def mk_world():
    """Two nodes, one edge, two hunks, signals for both."""
    h1 = mk_hunk("a.py:10", "a.py")
    h2 = mk_hunk("b.py:5", "b.py", patch="@@ -5,1 +5,2 @@\n+use_add()\n context")
    nodes = [
        DagNode(number=1, title="WriteBuffer class", hunk_ids=["a.py:10"],
                badge=Badge.CODE_CHANGE),
        DagNode(number=2, title="call site update", hunk_ids=["b.py:5"],
                badge=Badge.CODE_CHANGE_EFFECTS),
    ]
    edges = [DagEdge(src=1, dst=2, reason="add")]
    signals = {
        "a.py:10": HunkSignals(hunk_id="a.py:10", defines=["add"], blast_radius=12,
                               sensitive=["subprocess"], test_touched=False),
        "b.py:5": HunkSignals(hunk_id="b.py:5", uses=["add"], test_touched=True),
    }
    return nodes, edges, [h1, h2], signals


class AnnotateTest(unittest.TestCase):
    def setUp(self):
        self.cfg = Config()
        patcher = mock.patch("crux.llm.shutil.which", return_value="/usr/bin/claude")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_coercion_defaults_clamps_and_drops_unknown(self):
        nodes, edges, hunks, signals = mk_world()
        run, calls = fake_run([envelope(json.dumps(GOOD_INNER))])
        with mock.patch("crux.llm.subprocess.run", side_effect=run):
            ann = analyze.annotate(nodes, edges, hunks, signals,
                                   intent=None, previous=None, cfg=self.cfg)
        self.assertIsInstance(ann, Annotation)
        self.assertEqual(ann.summary, "Adds a write buffer in front of the recipe index.")
        self.assertEqual(len(ann.claims), 1)
        self.assertTrue(ann.claims[0].uncertain)
        # unknown node 7 dropped; node 1 kept with clamped minutes
        self.assertEqual(set(ann.nodes), {1, 2})
        self.assertEqual(ann.nodes[1].minutes, 10)
        self.assertTrue(ann.nodes[1].design_decision)
        # node 2 was missing from the reply -> default filled from DagNode
        self.assertEqual(ann.nodes[2].title, "call site update")
        self.assertEqual(ann.nodes[2].minutes, 2)
        self.assertEqual(ann.audit[0].verdict, Verdict.CONTRADICTED)

    def test_verdict_coercion_variants(self):
        inner = dict(GOOD_INNER)
        inner["audit"] = [
            {"claim": "x", "verdict": "could_not_verify", "evidence": ""},
            {"claim": "y", "verdict": "verified", "evidence": ""},
            {"claim": "z", "verdict": "garbage", "evidence": ""},
        ]
        nodes, edges, hunks, signals = mk_world()
        run, _ = fake_run([envelope(json.dumps(inner))])
        with mock.patch("crux.llm.subprocess.run", side_effect=run):
            ann = analyze.annotate(nodes, edges, hunks, signals, None, None, self.cfg)
        self.assertEqual([a.verdict for a in ann.audit],
                         [Verdict.COULD_NOT_VERIFY, Verdict.VERIFIED, Verdict.COULD_NOT_VERIFY])

    def test_malformed_then_valid_retry_through_annotate(self):
        nodes, edges, hunks, signals = mk_world()
        run, calls = fake_run([
            envelope("I could not produce JSON, sorry."),
            envelope(json.dumps(GOOD_INNER)),
        ])
        with mock.patch("crux.llm.subprocess.run", side_effect=run):
            ann = analyze.annotate(nodes, edges, hunks, signals, None, None, self.cfg)
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[1][1]["input"].endswith("Return ONLY the JSON object."))
        self.assertEqual(set(ann.nodes), {1, 2})

    def test_prompt_contents(self):
        nodes, edges, hunks, signals = mk_world()
        intent = {"summary": "buffered writes", "uncertainties": ["flush timing"]}
        run, calls = fake_run([envelope(json.dumps(GOOD_INNER))])
        with mock.patch("crux.llm.subprocess.run", side_effect=run):
            analyze.annotate(nodes, edges, hunks, signals, intent, None, self.cfg)
        prompt = calls[0][1]["input"]
        # sources
        self.assertIn("buffered writes", prompt)
        self.assertIn("flush timing", prompt)
        # DAG with numbers, titles, patches, edge reason
        self.assertIn("Node 1 · WriteBuffer class", prompt)
        self.assertIn("+def add(x):", prompt)
        self.assertIn("1 -> 2 (reason: add)", prompt)
        # chips
        self.assertIn("blast: 12 call sites", prompt)
        self.assertIn("sensitive: subprocess", prompt)
        self.assertIn("tests touch this", prompt)
        self.assertIn("no test coverage found", prompt)
        # output contract: overview is the star; summary + nodes also present
        self.assertIn('"summary"', prompt)
        self.assertIn('"overview"', prompt)
        self.assertIn("high-level", prompt.lower())
        # integration-test steps, hard-gated to big functionality only
        self.assertIn('"integration_test"', prompt)
        self.assertIn("USUALLY EMPTY", prompt)
        self.assertIn("SINGLE JSON object and NOTHING else", prompt)
        # template braces rendered: schema shows single-brace JSON, no stray {{
        self.assertIn('{\n  "summary":', prompt)
        self.assertNotIn("{{", prompt)

    def test_previous_review_fed_back_as_feedback(self):
        nodes, edges, hunks, signals = mk_world()
        previous = RunState(
            version=1, branch="feat", base_sha="b" * 7, head_sha="old",
            pr_number=3, fingerprints={},
            annotation=Annotation(
                summary="Old summary of the PR.",
                overview=["First big idea — `a.py:10`", "Second idea — `b.py:5`"]))
        run, calls = fake_run([envelope(json.dumps(GOOD_INNER))])
        with mock.patch("crux.llm.subprocess.run", side_effect=run):
            analyze.annotate(nodes, edges, hunks, signals, None, previous, self.cfg)
        prompt = calls[0][1]["input"]
        self.assertIn("Your earlier review of this PR", prompt)
        self.assertIn("Old summary of the PR.", prompt)
        self.assertIn("First big idea", prompt)
        # continuity, but equal weight — not recency-biased
        self.assertIn("EQUAL weight", prompt)

    def test_first_pass_has_no_previous_review(self):
        nodes, edges, hunks, signals = mk_world()
        run, calls = fake_run([envelope(json.dumps(GOOD_INNER))])
        with mock.patch("crux.llm.subprocess.run", side_effect=run):
            analyze.annotate(nodes, edges, hunks, signals, None, None, self.cfg)
        self.assertIn("no earlier review", calls[0][1]["input"])

    def test_patch_truncated_to_node_budget(self):
        big_patch = "@@ -1,200 +1,200 @@\n" + "\n".join(f"+line {i}" for i in range(200))
        hunk = mk_hunk("a.py:1", "a.py", patch=big_patch)
        nodes = [DagNode(number=1, title="big", hunk_ids=["a.py:1"], badge=Badge.CODE_CHANGE)]
        signals = {"a.py:1": HunkSignals(hunk_id="a.py:1")}
        run, calls = fake_run([envelope(json.dumps({"summary": "s", "nodes": {}}))])
        with mock.patch("crux.llm.subprocess.run", side_effect=run):
            analyze.annotate(nodes, [], [hunk], signals, None, None, self.cfg)
        prompt = calls[0][1]["input"]
        # Head AND tail shown, middle elided with an explicit
        # unread marker — never head-only.
        self.assertIn("+line 0", prompt)
        self.assertIn("+line 199", prompt)
        self.assertNotIn("+line 100", prompt)
        self.assertIn("NOT READ", prompt)
        # The prompt rules bind claims about unread regions.
        self.assertIn("Absence claims", prompt)

    def test_commit_subjects_in_prompt(self):
        nodes, edges, hunks, signals = mk_world()
        info = RepoInfo(root="/repo", branch="feat", head_sha="h" * 7, base_sha="b" * 7,
                        owner="example-org", repo="demo")
        # crux.analyze and crux.llm share the stdlib subprocess module, so one
        # patched subprocess.run dispatches on argv[0] for both callers.
        git_calls: list[list[str]] = []
        claude_calls: list[tuple[list[str], dict]] = []

        def dispatch(argv, **kwargs):
            if argv[0] == "git":
                git_calls.append(list(argv))
                # %B%x00: full messages, NUL-separated (D27). The second one
                # is Crux-amended — its Crux: subject must win over "fix".
                return SimpleNamespace(
                    returncode=0,
                    stdout=("Add WriteBuffer\n\x00"
                            "fix\n\nCrux: Wire flush on shutdown\n\n"
                            "Amended-by: Crux\n\x00"),
                    stderr="")
            if argv[0] == "gh":  # _pr_body probe; no PR exists yet
                return SimpleNamespace(returncode=1, stdout="", stderr="")
            claude_calls.append((list(argv), kwargs))
            return SimpleNamespace(
                returncode=0, stdout=envelope(json.dumps(GOOD_INNER)), stderr="")

        with mock.patch("subprocess.run", side_effect=dispatch):
            analyze.annotate(nodes, edges, hunks, signals, None, None, self.cfg, info=info)
        self.assertEqual(len(git_calls), 1)
        argv = git_calls[0]
        self.assertEqual(argv[:3], ["git", "log", "--format=%B%x00"])
        self.assertIn(f"{'b' * 7}..{'h' * 7}", argv)
        prompt = claude_calls[0][1]["input"]
        self.assertIn("- Add WriteBuffer", prompt)
        self.assertIn("- Wire flush on shutdown", prompt)
        # the terse human line of the amended commit is not what the prompt sees
        self.assertNotIn("- fix", prompt)

    def test_reuse_previous_annotations(self):
        nodes, edges, hunks, signals = mk_world()
        prev_node_ann = NodeAnnotation(
            number=1, title="prev falsifiable title", why="prev why cites blast.",
            questions=["prev q?"], chips=["blast: 12 call sites"], minutes=4,
            design_decision=False)
        previous = RunState(
            version=1, branch="feat", base_sha="b" * 7, head_sha="old", pr_number=None,
            fingerprints={"a.py:10": analyze.fingerprint(hunks[0]),
                          "b.py:5": "different-fingerprint"},
            nodes=[DagNode(number=1, title="WriteBuffer class", hunk_ids=["a.py:10"],
                           badge=Badge.CODE_CHANGE),
                   DagNode(number=2, title="call site update", hunk_ids=["b.py:5"],
                           badge=Badge.CODE_CHANGE_EFFECTS)],
            annotation=Annotation(summary="old summary",
                                  nodes={1: prev_node_ann,
                                         2: NodeAnnotation(number=2, title="old 2", why="old")}),
        )
        # model only answers for the changed node 2
        inner = {
            "summary": "updated",
            "claims": [],
            "nodes": {"2": {"title": "call sites now use add()", "why": "uses add.",
                            "minutes": 3}},
            "audit": [],
        }
        run, calls = fake_run([envelope(json.dumps(inner))])
        with mock.patch("crux.llm.subprocess.run", side_effect=run):
            ann = analyze.annotate(nodes, edges, hunks, signals, None, previous, self.cfg)
        # D9 reuse is applied after the model call regardless of the prompt:
        # node 1 annotation is the previous one, verbatim; node 1 marked reused
        self.assertEqual(ann.nodes[1].title, "prev falsifiable title")
        self.assertEqual(ann.nodes[1].why, "prev why cites blast.")
        self.assertEqual(ann.nodes[1].questions, ["prev q?"])
        self.assertEqual(ann.nodes[1].minutes, 4)
        self.assertTrue(nodes[0].reused)
        self.assertFalse(nodes[1].reused)
        # node 2 got a fresh annotation
        self.assertEqual(ann.nodes[2].title, "call sites now use add()")
        self.assertEqual(ann.nodes[2].minutes, 3)

    def test_reuse_survives_line_number_drift(self):
        """D9: hunk ids embed new_start, so an insertion earlier in the file
        changes every downstream id. Reuse must match by fingerprint, not id."""
        nodes, edges, hunks, signals = mk_world()
        # Same content as the current hunks, seen 5 lines away last push:
        # identical fingerprints, different hunk ids.
        prev_h1 = mk_hunk("a.py:5", "a.py")
        prev_h2 = mk_hunk("b.py:99", "b.py",
                          patch="@@ -99,1 +99,2 @@\n+use_add()\n context")
        self.assertEqual(analyze.fingerprint(prev_h1), analyze.fingerprint(hunks[0]))
        self.assertEqual(analyze.fingerprint(prev_h2), analyze.fingerprint(hunks[1]))
        previous = RunState(
            version=1, branch="feat", base_sha="b" * 7, head_sha="old", pr_number=None,
            fingerprints={"a.py:5": analyze.fingerprint(prev_h1),
                          "b.py:99": analyze.fingerprint(prev_h2)},
            nodes=[DagNode(number=1, title="WriteBuffer class", hunk_ids=["a.py:5"],
                           badge=Badge.CODE_CHANGE),
                   DagNode(number=2, title="call site update", hunk_ids=["b.py:99"],
                           badge=Badge.CODE_CHANGE_EFFECTS)],
            annotation=Annotation(
                summary="old",
                nodes={1: NodeAnnotation(number=1, title="drifted title 1", why="w1"),
                       2: NodeAnnotation(number=2, title="drifted title 2", why="w2")}),
        )
        run, calls = fake_run([envelope(json.dumps({"summary": "s", "nodes": {}}))])
        with mock.patch("crux.llm.subprocess.run", side_effect=run):
            ann = analyze.annotate(nodes, edges, hunks, signals, None, previous, self.cfg)
        # both nodes reused despite every hunk id having drifted
        self.assertEqual(ann.nodes[1].title, "drifted title 1")
        self.assertEqual(ann.nodes[2].title, "drifted title 2")
        self.assertTrue(nodes[0].reused)
        self.assertTrue(nodes[1].reused)

    def test_integration_test_steps_coerced_and_capped(self):
        nodes, edges, hunks, signals = mk_world()
        inner = {"summary": "s",
                 "integration_test": ["step a", "  step b  ", ""]
                 + [f"extra {i}" for i in range(12)]}
        run, _ = fake_run([envelope(json.dumps(inner))])
        with mock.patch("crux.llm.subprocess.run", side_effect=run):
            ann = analyze.annotate(nodes, edges, hunks, signals, None, None, self.cfg)
        self.assertEqual(ann.integration_test[:2], ["step a", "step b"])
        self.assertLessEqual(len(ann.integration_test), 10)

    def test_suggested_tier_coerced_and_garbage_dropped(self):
        nodes, edges, hunks, signals = mk_world()
        inner = {
            "summary": "s", "claims": [], "audit": [],
            "nodes": {"1": {"title": "t", "why": "w", "suggested_tier": "RED"},
                      "2": {"title": "t2", "why": "w2", "suggested_tier": "purple"}},
        }
        run, _ = fake_run([envelope(json.dumps(inner))])
        with mock.patch("crux.llm.subprocess.run", side_effect=run):
            ann = analyze.annotate(nodes, edges, hunks, signals, None, None, self.cfg)
        self.assertEqual(ann.nodes[1].suggested_tier, "red")
        self.assertEqual(ann.nodes[2].suggested_tier, "")

    def test_prompt_carries_plain_english_contract(self):
        nodes, edges, hunks, signals = mk_world()
        run, calls = fake_run([envelope(json.dumps(GOOD_INNER))])
        with mock.patch("crux.llm.subprocess.run", side_effect=run):
            analyze.annotate(nodes, edges, hunks, signals, None, None, self.cfg)
        prompt = calls[0][1]["input"]
        # D15: the plain-English rule is stated explicitly and up front
        self.assertIn("PLAIN", prompt)
        self.assertIn("no tool jargon", prompt)
        self.assertIn('"blast radius" → "used in N places"', prompt)
        self.assertIn("grade-8", prompt)
        self.assertIn("suggested_tier", prompt)

    def test_no_design_key_gives_empty_design_list(self):
        nodes, edges, hunks, signals = mk_world()
        run, _ = fake_run([envelope(json.dumps(GOOD_INNER))])
        with mock.patch("crux.llm.subprocess.run", side_effect=run):
            ann = analyze.annotate(nodes, edges, hunks, signals, None, None, self.cfg)
        self.assertEqual(ann.design, [])

    def test_fingerprint_ignores_header_but_not_content(self):
        h1 = mk_hunk(patch="@@ -10,3 +10,5 @@\n+x = 1\n context")
        h2 = mk_hunk(patch="@@ -99,3 +104,5 @@\n+x = 1\n context")  # only offsets differ
        h3 = mk_hunk(patch="@@ -10,3 +10,5 @@\n+x = 2\n context")
        self.assertEqual(analyze.fingerprint(h1), analyze.fingerprint(h2))
        self.assertNotEqual(analyze.fingerprint(h1), analyze.fingerprint(h3))


class TruncationHeadTailTest(unittest.TestCase):
    """Over-budget patches are shown head + tail with an explicit
    unread marker naming the elided line range, and the tail's share grows
    when the elided region contains test markers — so the model never again
    asserts "no tests" about a tail it was not shown."""

    @staticmethod
    def _hunk(n: int = 200, new_start: int = 1,
              edits: dict[int, str] | None = None) -> Hunk:
        body = [f"+line {i}" for i in range(n)]
        for i, text in (edits or {}).items():
            body[i] = text
        return Hunk(
            id="f.py:1", file="f.py", old_start=0, old_count=0,
            new_start=new_start, new_count=n,
            patch=f"@@ -0,0 +1,{n} @@\n" + "\n".join(body))

    def test_head_tail_shape_within_budget(self):
        hunk = self._hunk()
        patch_lines = hunk.patch.splitlines()  # 201: header + 200 adds
        out = analyze._truncated_patch(hunk, patch_lines, 80)
        markers = [l for l in out if "NOT READ" in l]
        self.assertEqual(len(markers), 1)
        # Exactly the budget's worth of real patch lines, split head + tail.
        self.assertEqual(len(out) - 1, 80)
        self.assertEqual(out[0], "@@ -0,0 +1,200 @@")  # head starts at the top
        self.assertEqual(out[-1], "+line 199")          # tail reaches the end
        # Default split: tail gets 1/4 of the budget (20 lines).
        self.assertEqual(out[-20], "+line 180")
        self.assertNotIn("+line 100", out)

    def test_marker_names_unread_line_range(self):
        hunk = self._hunk()
        patch_lines = hunk.patch.splitlines()
        out = analyze._truncated_patch(hunk, patch_lines, 80)
        marker = next(l for l in out if "NOT READ" in l)
        # head = 60 patch lines (header + adds 0-58 = new-file lines 1-59),
        # tail = last 20 adds (lines 181-200): unread is lines 60-180.
        self.assertIn("lines 60-180 of f.py", marker)
        self.assertIn("(121 patch lines)", marker)
        self.assertIn("UNKNOWN", marker)

    def test_test_markers_in_tail_grow_tail_share(self):
        hunk = self._hunk(edits={190: "+def test_debug_never_renders_content():"})
        patch_lines = hunk.patch.splitlines()
        out = analyze._truncated_patch(hunk, patch_lines, 80)
        # Tail share grows to 1/2 (40 lines), still within the same budget.
        self.assertEqual(len(out) - 1, 80)
        self.assertIn("+def test_debug_never_renders_content():", out)
        self.assertIn("+line 160", out)     # tail now starts earlier
        marker = next(l for l in out if "NOT READ" in l)
        self.assertIn("lines 40-160 of f.py", marker)

    def test_marker_detects_rust_and_js_test_markers(self):
        for text in ("+#[cfg(test)]", "+mod tests {", "+describe('x', () => {",
                     "+@Test", "+func TestThing(t *testing.T) {"):
            hunk = self._hunk(edits={195: text})
            out = analyze._truncated_patch(hunk, hunk.patch.splitlines(), 80)
            self.assertIn(text, out, text)
        # No false positive on a word merely containing "it(".
        hunk = self._hunk(edits={195: "+    unit(x)"})
        out = analyze._truncated_patch(hunk, hunk.patch.splitlines(), 80)
        self.assertNotIn("+line 165", out)  # tail stayed at the default 20

    def test_pure_deletion_marker_has_no_line_range(self):
        patch = "@@ -1,120 +0,0 @@\n" + "\n".join(f"-old {i}" for i in range(120))
        hunk = Hunk(id="g.py:0", file="g.py", old_start=1, old_count=120,
                    new_start=0, new_count=0, patch=patch)
        out = analyze._truncated_patch(hunk, patch.splitlines(), 80)
        marker = next(l for l in out if "NOT READ" in l)
        self.assertIn("removed lines of g.py", marker)

    def test_budget_exhausted_hunk_marked_unread_with_range(self):
        big = self._hunk()
        second = Hunk(
            id="h.py:5", file="h.py", old_start=0, old_count=0,
            new_start=5, new_count=30,
            patch="@@ -0,0 +5,30 @@\n" + "\n".join(f"+h {i}" for i in range(30)))
        node = DagNode(number=1, title="two hunks",
                       hunk_ids=["f.py:1", "h.py:5"], badge=Badge.CODE_CHANGE)
        block = analyze._node_block(
            node, {"f.py:1": big, "h.py:5": second}, large_cap=50)
        self.assertIn("node budget exhausted", block)
        self.assertIn("lines 5-34 of h.py", block)
        self.assertNotIn("+h 0", block)


class DesignFindingsTest(unittest.TestCase):
    """D14: CLAUDE.md + standards rules in the prompt; validator drops
    citation-less findings, clamps to standards_max, never touches tiers."""

    def setUp(self):
        self.cfg = Config(standards_rules=["Prefer composition over inheritance"])
        patcher = mock.patch("crux.llm.shutil.which", return_value="/usr/bin/claude")
        patcher.start()
        self.addCleanup(patcher.stop)

    # Design findings are no longer requested in the prompt, but the validator
    # is kept: if the model volunteers a `design` array, it is still coerced,
    # citation-checked, and capped. The prompt-side design tests are gone.

    # -- validator side ------------------------------------------------------

    def _annotate_with_design(self, design: list[dict], cfg: Config) -> Annotation:
        nodes, edges, hunks, signals = mk_world()
        inner = {"summary": "s", "claims": [], "nodes": {}, "audit": [],
                 "design": design}
        run, _ = fake_run([envelope(json.dumps(inner))])
        with mock.patch("crux.llm.subprocess.run", side_effect=run):
            return analyze.annotate(nodes, edges, hunks, signals, None, None, cfg)

    def test_citation_less_findings_dropped_and_fields_coerced(self):
        ann = self._annotate_with_design([
            {"kind": "oo-design", "text": "no citation here", "file": "a.py",
             "line_start": 1, "line_end": 2,
             "convention_source": "CLAUDE.md", "convention_ref": ""},
            {"kind": "oo-design", "text": "no ref key at all", "file": "a.py",
             "line_start": 1, "line_end": 2, "convention_source": "CLAUDE.md"},
            {"kind": "oo-design", "text": "no offending file",
             "convention_source": "CLAUDE.md", "convention_ref": "some rule"},
            {"kind": "WEIRD", "text": "kept", "file": "b.py",
             "line_start": "7", "line_end": 3,   # end < start must clamp up
             "convention_source": "", "convention_ref": "util/retry.py:12"},
        ], self.cfg)
        self.assertEqual(len(ann.design), 1)
        f = ann.design[0]
        self.assertIsInstance(f, DesignFinding)
        self.assertEqual(f.kind, "convention")        # unknown kind normalized
        self.assertEqual(f.text, "kept")
        self.assertEqual(f.file, "b.py")
        self.assertEqual((f.line_start, f.line_end), (7, 7))
        self.assertEqual(f.convention_source, "sibling code")  # inferred from ref
        self.assertEqual(f.convention_ref, "util/retry.py:12")

    def test_cap_enforced_at_standards_max(self):
        cfg = Config(standards_max=2)
        raw = [{"kind": "convention", "text": f"finding {i}", "file": "a.py",
                "line_start": i, "line_end": i,
                "convention_source": "crux.toml", "convention_ref": f"rule {i}"}
               for i in range(1, 5)]
        ann = self._annotate_with_design(raw, cfg)
        self.assertEqual(len(ann.design), 2)
        self.assertEqual([f.text for f in ann.design], ["finding 1", "finding 2"])

    def test_cap_applies_after_citation_drop(self):
        cfg = Config(standards_max=2)
        raw = [
            {"kind": "convention", "text": "dropped", "file": "a.py",
             "line_start": 1, "line_end": 1,
             "convention_source": "crux.toml", "convention_ref": ""},
            {"kind": "convention", "text": "kept 1", "file": "a.py",
             "line_start": 2, "line_end": 2,
             "convention_source": "crux.toml", "convention_ref": "r1"},
            {"kind": "convention", "text": "kept 2", "file": "a.py",
             "line_start": 3, "line_end": 3,
             "convention_source": "crux.toml", "convention_ref": "r2"},
        ]
        ann = self._annotate_with_design(raw, cfg)
        self.assertEqual([f.text for f in ann.design], ["kept 1", "kept 2"])


class TestJargonRetryD15(unittest.TestCase):
    def setUp(self):
        self.cfg = Config()
        patcher = mock.patch("crux.llm.shutil.which", return_value="/usr/bin/claude")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _inner(self, why: str) -> str:
        inner = copy.deepcopy(GOOD_INNER)
        inner["nodes"]["1"]["why"] = why
        return json.dumps(inner)

    def test_jargon_triggers_exactly_one_retry(self):
        nodes, edges, hunks, signals = mk_world()
        run, calls = fake_run([
            envelope(self._inner("This hunk is the root of the DAG.")),
            envelope(self._inner("This changed chunk is the heart of the PR.")),
        ])
        with mock.patch("crux.llm.subprocess.run", side_effect=run):
            ann = analyze.annotate(nodes, edges, hunks, signals, None, None, self.cfg)
        self.assertEqual(len(calls), 2)
        self.assertIn("REWRITE REQUIRED", calls[1][1].get("input", ""))
        self.assertEqual(ann.nodes[1].why, "This changed chunk is the heart of the PR.")

    def test_jargon_kept_but_logged_when_retry_fails(self):
        nodes, edges, hunks, signals = mk_world()
        bad = self._inner("Topological order of the DAG.")
        run, calls = fake_run([envelope(bad), envelope(bad)])
        with mock.patch("crux.llm.subprocess.run", side_effect=run):
            with self.assertLogs("crux.analyze", level="WARNING") as captured:
                ann = analyze.annotate(nodes, edges, hunks, signals, None, None, self.cfg)
        self.assertEqual(len(calls), 2)  # never a third call
        self.assertIn("Topological order of the DAG.", ann.nodes[1].why)
        self.assertTrue(any("jargon kept after retry" in m for m in captured.output))


class TestBreakdownAndBeforeAfter(unittest.TestCase):
    """D23 (50-line breakdown) + D24 (Crux does the before/after) + D25
    (tests ride along) — prompt contract and response coercion."""

    def setUp(self):
        self.cfg = Config()
        patcher = mock.patch("crux.llm.shutil.which", return_value="/usr/bin/claude")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _annotate(self, inner: dict, nodes=None, edges=None, hunks=None,
                  signals=None, previous=None):
        if nodes is None:
            nodes, edges, hunks, signals = mk_world()
        run, calls = fake_run([envelope(json.dumps(inner))])
        with mock.patch("crux.llm.subprocess.run", side_effect=run):
            ann = analyze.annotate(nodes, edges, hunks, signals, None,
                                   previous, self.cfg)
        return ann, calls

    def test_before_after_and_breakdown_coerced(self):
        inner = {
            "summary": "s",
            "nodes": {"1": {
                "title": "t", "why": "w",
                "before": "  writes went straight to the database  ",
                "now": "writes queue in a buffer first",   # `now` alias accepted
                "breakdown": ["part one — `a.py:1-40`", "  ", "part two — `a.py:41-80`"],
            }},
        }
        ann, _ = self._annotate(inner)
        self.assertEqual(ann.nodes[1].before, "writes went straight to the database")
        self.assertEqual(ann.nodes[1].after, "writes queue in a buffer first")
        self.assertEqual(ann.nodes[1].breakdown,
                         ["part one — `a.py:1-40`", "part two — `a.py:41-80`"])

    def test_breakdown_capped_at_six_parts(self):
        inner = {"summary": "s", "nodes": {"1": {
            "title": "t", "why": "w",
            "breakdown": [f"part {i} — `a.py:{i}`" for i in range(1, 12)]}}}
        ann, _ = self._annotate(inner)
        self.assertEqual(len(ann.nodes[1].breakdown), 6)

    def test_missing_fields_default_empty(self):
        ann, _ = self._annotate({"summary": "s", "nodes": {"1": {"title": "t"}}})
        self.assertEqual(ann.nodes[1].before, "")
        self.assertEqual(ann.nodes[1].after, "")
        self.assertEqual(ann.nodes[1].breakdown, [])

    def test_large_node_marked_in_prompt(self):
        big_patch = "@@ -1,2 +1,80 @@\n" + "\n".join(f"+line {i}" for i in range(80))
        hunk = mk_hunk("a.py:1", "a.py", patch=big_patch)
        nodes = [DagNode(number=1, title="big", hunk_ids=["a.py:1"],
                         badge=Badge.CODE_CHANGE)]
        _, calls = self._annotate({"summary": "s", "nodes": {}},
                                  nodes=nodes, edges=[], hunks=[hunk],
                                  signals={"a.py:1": HunkSignals(hunk_id="a.py:1")})
        prompt = calls[0][1]["input"]
        self.assertIn("[changed lines: 80]", prompt)
        self.assertIn('LARGE — "breakdown" REQUIRED', prompt)

    def test_small_node_not_marked_large(self):
        nodes, edges, hunks, signals = mk_world()
        _, calls = self._annotate({"summary": "s", "nodes": {}}, nodes=nodes,
                                  edges=edges, hunks=hunks, signals=signals)
        prompt = calls[0][1]["input"]
        self.assertIn("[changed lines:", prompt)
        self.assertNotIn("LARGE", prompt.split("## Output format")[0])

    def test_prompt_states_new_contracts(self):
        nodes, edges, hunks, signals = mk_world()
        _, calls = self._annotate({"summary": "s"}, nodes=nodes, edges=edges,
                                  hunks=hunks, signals=signals)
        prompt = calls[0][1]["input"]
        # D23: never a wall of code; 50-line parts
        self.assertIn("NEVER send the reviewer to read a wall of code", prompt)
        self.assertIn("AT MOST 50 lines", prompt)
        # D24: Crux does the comparison
        self.assertIn("NEVER ask the reviewer to compare versions", prompt)
        self.assertIn('"before"', prompt)
        self.assertIn('"after"', prompt)
        # D25: tests ride along
        self.assertIn("Tests ride along", prompt)

    def test_reuse_carries_before_after_breakdown(self):
        nodes, edges, hunks, signals = mk_world()
        prev = NodeAnnotation(
            number=1, title="prev", why="w",
            before="old behavior", after="new behavior",
            breakdown=["part — `a.py:1-30`"])
        previous = RunState(
            version=1, branch="feat", base_sha="b" * 7, head_sha="old",
            pr_number=None,
            fingerprints={"a.py:10": analyze.fingerprint(hunks[0])},
            nodes=[DagNode(number=1, title="WriteBuffer class",
                           hunk_ids=["a.py:10"], badge=Badge.CODE_CHANGE)],
            annotation=Annotation(summary="old", nodes={1: prev}),
        )
        ann, calls = self._annotate({"summary": "s", "nodes": {}},
                                    nodes=nodes, edges=edges, hunks=hunks,
                                    signals=signals, previous=previous)
        self.assertEqual(ann.nodes[1].before, "old behavior")
        self.assertEqual(ann.nodes[1].after, "new behavior")
        self.assertEqual(ann.nodes[1].breakdown, ["part — `a.py:1-30`"])
        # and the reuse block in the prompt shows them verbatim
        self.assertIn('"breakdown"', calls[0][1]["input"])

    def test_jargon_in_breakdown_triggers_retry(self):
        nodes, edges, hunks, signals = mk_world()
        bad = {"summary": "s", "nodes": {"1": {
            "title": "t", "why": "w",
            "breakdown": ["the DAG root lives here — `a.py:1-20`"]}}}
        good = {"summary": "s", "nodes": {"1": {
            "title": "t", "why": "w",
            "breakdown": ["the heart of the change lives here — `a.py:1-20`"]}}}
        run, calls = fake_run([envelope(json.dumps(bad)), envelope(json.dumps(good))])
        with mock.patch("crux.llm.subprocess.run", side_effect=run):
            ann = analyze.annotate(nodes, edges, hunks, signals, None, None, self.cfg)
        self.assertEqual(len(calls), 2)
        self.assertIn("heart of the change", ann.nodes[1].breakdown[0])


class TestValidationGatesD29(unittest.TestCase):
    """analyze.validate_annotation: deterministic repairs at the end of the
    LLM step — the card is held to the directives even when the model (or a
    reused cached annotation) ignores the prompt."""

    def setUp(self) -> None:
        self.cfg = Config()
        self.test_hunk = mk_hunk("tests/test_a.py:10", "tests/test_a.py")
        self.code_hunk = mk_hunk("a.py:10", "a.py")
        self.nodes = [
            DagNode(number=1, title="tests", hunk_ids=["tests/test_a.py:10"],
                    badge=Badge.CODE_CHANGE_EFFECTS),
            DagNode(number=2, title="code", hunk_ids=["a.py:10"],
                    badge=Badge.CODE_CHANGE),
        ]
        self.hunks = [self.test_hunk, self.code_hunk]

    def _annotation(self, **overview) -> Annotation:
        return Annotation(
            summary="s",
            overview=overview.get("overview", []),
            nodes={
                1: NodeAnnotation(number=1, title="t", why="",
                                  suggested_tier="red"),
                2: NodeAnnotation(number=2, title="c", why="",
                                  suggested_tier="red"),
            })

    def test_red_suggestion_on_test_only_change_is_demoted(self) -> None:
        ann = self._annotation()
        repairs = analyze.validate_annotation(ann, self.nodes, self.hunks, self.cfg)
        self.assertEqual(ann.nodes[1].suggested_tier, "yellow")
        self.assertTrue(any("demoted" in r for r in repairs))

    def test_red_suggestion_on_code_change_is_untouched(self) -> None:
        ann = self._annotation()
        analyze.validate_annotation(ann, self.nodes, self.hunks, self.cfg)
        self.assertEqual(ann.nodes[2].suggested_tier, "red")

    def test_overview_bullet_pointing_only_at_tests_is_dropped(self) -> None:
        ann = self._annotation(overview=[
            "New tests lock in the tier rules — `tests/test_a.py:10`",
            "The buffer batches writes — `a.py:10`",
        ])
        repairs = analyze.validate_annotation(ann, self.nodes, self.hunks, self.cfg)
        self.assertEqual(ann.overview,
                         ["The buffer batches writes — `a.py:10`"])
        self.assertTrue(any("big-picture" in r for r in repairs))

    def test_overview_bullet_with_mixed_pointers_is_kept(self) -> None:
        bullet = "Covered by tests — `a.py:10` and `tests/test_a.py:10`"
        ann = self._annotation(overview=[bullet])
        analyze.validate_annotation(ann, self.nodes, self.hunks, self.cfg)
        self.assertEqual(ann.overview, [bullet])

    def test_bullet_without_pointers_is_kept(self) -> None:
        ann = self._annotation(overview=["A plain idea with no pointer"])
        analyze.validate_annotation(ann, self.nodes, self.hunks, self.cfg)
        self.assertEqual(ann.overview, ["A plain idea with no pointer"])

    def test_no_repairs_returns_empty_list(self) -> None:
        ann = Annotation(summary="s", nodes={
            2: NodeAnnotation(number=2, title="c", why="", suggested_tier="red")})
        self.assertEqual(
            analyze.validate_annotation(ann, self.nodes, self.hunks, self.cfg), [])


class TestShortCardD32(unittest.TestCase):
    """D32: the card is written to word budgets, and the labels it prints are
    never repeated back inside the text they label."""

    def setUp(self) -> None:
        self.cfg = Config()
        patcher = mock.patch("crux.llm.shutil.which", return_value="/usr/bin/claude")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _annotate(self, inner: dict):
        nodes, edges, hunks, signals = mk_world()
        run, calls = fake_run([envelope(json.dumps(inner))])
        with mock.patch("crux.llm.subprocess.run", side_effect=run):
            ann = analyze.annotate(nodes, edges, hunks, signals, None, None,
                                   self.cfg)
        return ann, calls

    def test_label_lead_stripped_from_before_and_after(self) -> None:
        # The card prints "**Before:** … **Now:** …" itself, so a model that
        # repeats the label produces "Before: Before, the code did X".
        ann, _ = self._annotate({"summary": "s", "nodes": {"1": {
            "title": "t",
            "before": "Before, writes went straight to the database",
            "after": "Now writes queue in a buffer first",
        }}})
        self.assertEqual(ann.nodes[1].before,
                         "writes went straight to the database")
        self.assertEqual(ann.nodes[1].after, "writes queue in a buffer first")

    def test_other_lead_words_stripped(self) -> None:
        for lead, rest in (("Previously, ", "the flush ran on every write"),
                           ("Prior to this change: ", "nothing drained the queue"),
                           ("Currently ", "the queue drains on shutdown")):
            ann, _ = self._annotate({"summary": "s", "nodes": {
                "1": {"title": "t", "before": lead + rest}}})
            self.assertEqual(ann.nodes[1].before, rest)

    def test_short_value_is_left_alone(self) -> None:
        # Nothing sensible survives the strip, so the model's words stand.
        ann, _ = self._annotate({"summary": "s", "nodes": {
            "1": {"title": "t", "before": "Before the flush"}}})
        self.assertEqual(ann.nodes[1].before, "Before the flush")

    def test_omitted_change_is_marked(self) -> None:
        # The model wrote up node 1 and left node 2 out; only node 2 is marked
        # (tiers reads this to keep an unwritten change out of must-read).
        ann, _ = self._annotate({"summary": "s", "nodes": {
            "1": {"title": "Batches writes through a buffer", "why": "w"}}})
        self.assertFalse(ann.nodes[1].omitted_by_model)
        self.assertTrue(ann.nodes[2].omitted_by_model)

    def test_word_budget_lint_reports_only_over_budget_fields(self) -> None:
        ann = Annotation(
            summary="A short summary.",
            overview=["A tidy idea — `a.py:1`",
                      " ".join(["word"] * 40) + " — `a.py:2`"],
            nodes={1: NodeAnnotation(number=1, title="A plain short title",
                                     why=" ".join(["word"] * 30))},
        )
        over = analyze.lint_word_budgets(ann)
        self.assertEqual(len(over), 2)
        self.assertTrue(any(o.startswith("overview bullet 40 words") for o in over))
        self.assertTrue(any(o.startswith("why 30 words") for o in over))

    def test_pointers_do_not_count_against_the_budget(self) -> None:
        bullet = " ".join(["word"] * 25) + " — `some/very/long/path.py:10-88`"
        self.assertEqual(
            analyze.lint_word_budgets(Annotation(summary="", overview=[bullet])), [])

    def test_prompt_states_the_word_budgets(self) -> None:
        _, calls = self._annotate({"summary": "s"})
        prompt = calls[0][1]["input"]
        self.assertIn("BRIEF, TO A BUDGET", prompt)
        self.assertIn("SAY IT ONCE", prompt)
        self.assertIn("| each `overview` bullet | 25 words |", prompt)
        self.assertIn("never a bare file or symbol name", prompt)


class TestChangeMapD33(unittest.TestCase):
    """D33: the card's change map is the review's own user-level flow — a
    handful of steps naming what happens, never the code's own structure."""

    def setUp(self) -> None:
        self.cfg = Config()
        patcher = mock.patch("crux.llm.shutil.which", return_value="/usr/bin/claude")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _annotate(self, inner: dict):
        nodes, edges, hunks, signals = mk_world()
        run, calls = fake_run([envelope(json.dumps(inner))])
        with mock.patch("crux.llm.subprocess.run", side_effect=run):
            ann = analyze.annotate(nodes, edges, hunks, signals, None, None,
                                   self.cfg)
        return ann, calls

    @staticmethod
    def _map(steps: list[tuple[str, str]], arrows: list[tuple[str, str]]) -> dict:
        return {"summary": "s", "change_map": {
            "steps": [{"id": i, "label": l} for i, l in steps],
            "arrows": [{"from": a, "to": b} for a, b in arrows]}}

    def test_steps_and_arrows_coerced(self) -> None:
        ann, _ = self._annotate({"summary": "s", "change_map": {
            "steps": [{"id": "push", "label": "Developer pushes a branch"},
                      {"id": "post", "label": " Review  posted on the PR "}],
            "arrows": [{"from": "push", "to": "post", "label": "when not trivial"}]}})
        cmap = ann.change_map
        self.assertEqual([s.id for s in cmap.steps], ["push", "post"])
        self.assertEqual(cmap.steps[1].label, "Review posted on the PR")
        self.assertEqual((cmap.arrows[0].src, cmap.arrows[0].dst),
                         ("push", "post"))
        self.assertEqual(cmap.arrows[0].label, "when not trivial")

    def test_no_map_key_leaves_it_unset(self) -> None:
        ann, _ = self._annotate({"summary": "s"})
        self.assertIsNone(ann.change_map)

    def test_empty_map_stays_unset(self) -> None:
        ann, _ = self._annotate({"summary": "s",
                                 "change_map": {"steps": [], "arrows": []}})
        self.assertIsNone(ann.change_map)

    def test_steps_capped_and_dangling_arrows_dropped(self) -> None:
        steps = [(f"s{i}", f"Something happens number {i}") for i in range(1, 12)]
        arrows = [(f"s{i}", f"s{i + 1}") for i in range(1, 11)]
        ann, _ = self._annotate(self._map(steps, arrows))
        self.assertEqual(len(ann.change_map.steps), analyze._MAP_STEPS_MAX)
        kept = {s.id for s in ann.change_map.steps}
        for arrow in ann.change_map.arrows:
            self.assertIn(arrow.src, kept)
            self.assertIn(arrow.dst, kept)

    def test_unusable_steps_and_arrows_dropped(self) -> None:
        ann, _ = self._annotate({"summary": "s", "change_map": {
            "steps": [{"id": "a", "label": "A thing happens"},
                      {"id": "a", "label": "Duplicate id thing"},
                      {"id": "", "label": "No id at all"},
                      {"id": "b", "label": ""},
                      {"id": "c", "label": "Another thing happens"}],
            "arrows": [{"from": "a", "to": "c"},
                       {"from": "a", "to": "c"},      # duplicate
                       {"from": "a", "to": "a"},      # self-loop
                       {"from": "a", "to": "gone"}]}})  # dangling
        self.assertEqual([s.id for s in ann.change_map.steps], ["a", "c"])
        self.assertEqual([(a.src, a.dst) for a in ann.change_map.arrows],
                         [("a", "c")])

    def test_code_level_map_is_dropped_whole(self) -> None:
        # One box named after the code and the whole picture goes: cutting the
        # box alone would break the flow the rest of the map draws.
        ann, _ = self._annotate(self._map(
            [("a", "Developer pushes a branch"), ("b", "post.py additions")],
            [("a", "b")]))
        self.assertIsNone(ann.change_map)

    def test_plain_english_map_survives_the_gate(self) -> None:
        ann, _ = self._annotate(self._map(
            [("a", "Developer pushes a branch"),
             ("b", "Review posted on the PR")],
            [("a", "b")]))
        self.assertEqual(len(ann.change_map.steps), 2)

    def test_code_label_detection(self) -> None:
        for label in ("post.py additions", "_pr_body", "render_card()",
                      "WriteBuffer", "crux/render.py", "handlePush"):
            self.assertTrue(analyze._is_code_label(label), label)
        for label in ("Developer pushes a branch", "Trivial changes skipped",
                      "Crux reads the diff", "Team told in Slack",
                      "Settings read from crux.toml",
                      "Merged",  # short plain steps carry no code at all
                      "PR opened"):
            self.assertFalse(analyze._is_code_label(label), label)

    def test_jargon_in_a_step_triggers_the_retry(self) -> None:
        nodes, edges, hunks, signals = mk_world()
        bad = self._map([("a", "Every hunk becomes a step"),
                         ("b", "Card posted on the PR")], [("a", "b")])
        good = self._map([("a", "Developer pushes a branch"),
                          ("b", "Card posted on the PR")], [("a", "b")])
        run, calls = fake_run([envelope(json.dumps(bad)),
                               envelope(json.dumps(good))])
        with mock.patch("crux.llm.subprocess.run", side_effect=run):
            ann = analyze.annotate(nodes, edges, hunks, signals, None, None,
                                   self.cfg)
        self.assertEqual(len(calls), 2)
        self.assertIn("REWRITE REQUIRED", calls[1][1]["input"])
        self.assertEqual(ann.change_map.steps[0].label,
                         "Developer pushes a branch")

    def test_long_labels_are_linted(self) -> None:
        ann = Annotation(summary="", change_map=ChangeMap(
            steps=[MapStep(id="a", label="One two three four five six seven")],
            arrows=[MapArrow(src="a", dst="a",
                             label="one two three four five")]))
        over = analyze.lint_word_budgets(ann)
        self.assertTrue(any(o.startswith("map step 7 words") for o in over))
        self.assertTrue(any(o.startswith("map arrow 5 words") for o in over))

    def test_prompt_demands_a_user_level_map(self) -> None:
        _, calls = self._annotate({"summary": "s"})
        prompt = calls[0][1]["input"]
        self.assertIn('"change_map"', prompt)
        self.assertIn("3-6 steps. NEVER more than 8", prompt)
        self.assertIn("A step is something that HAPPENS", prompt)


class TestPrBodyRestFallback(unittest.TestCase):
    """`_pr_body` on a host with no gh — the gh-less PR-description path.

    Untested until now: replacing _pr_body_rest's whole body with `return ""`
    left the suite green, i.e. the entire cloud-session PR-description feed
    could be deleted without a single failure. These pin it.
    """

    def _info(self) -> RepoInfo:
        return RepoInfo(root="/repo", branch="feat/x", head_sha="h" * 40,
                        base_sha="b" * 40, owner="example-org", repo="crux")

    def test_no_gh_reads_the_description_through_the_rest_fallback(self) -> None:
        info = self._info()
        with mock.patch("crux.analyze.subprocess.run",
                        side_effect=FileNotFoundError), \
             mock.patch("crux.post.find_pr", return_value=7), \
             mock.patch("crux.post._run_gh",
                        return_value=json.dumps({"body": " Why this PR \n"})) as gh:
            self.assertEqual(analyze._pr_body(info), "Why this PR")
        self.assertEqual(gh.call_args.args[0],
                         ["api", "repos/example-org/crux/pulls/7"])

    def test_no_pr_yet_is_empty_not_a_crash(self) -> None:
        info = self._info()
        with mock.patch("crux.analyze.subprocess.run",
                        side_effect=FileNotFoundError), \
             mock.patch("crux.post.find_pr", return_value=None):
            self.assertEqual(analyze._pr_body(info), "")

    def test_no_token_degrades_to_empty(self) -> None:
        """The common cloud-container state: no gh AND no token. The
        description is a nice-to-have, so it must degrade to "" rather than
        take the whole annotate step down."""
        info = self._info()
        with mock.patch("crux.analyze.subprocess.run",
                        side_effect=FileNotFoundError), \
             mock.patch("crux.post.find_pr",
                        side_effect=PostError("gh not installed and no "
                                              "GitHub token found")):
            self.assertEqual(analyze._pr_body(info), "")


if __name__ == "__main__":
    unittest.main()
