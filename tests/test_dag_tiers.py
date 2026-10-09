# Copyright © 2026 Sonomos, Inc.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for crux.dag and crux.tiers (stdlib unittest, no subprocess use)."""
from __future__ import annotations

import unittest

from crux import dag, tiers
from crux.models import (
    Annotation,
    Badge,
    Claim,
    Config,
    DagEdge,
    DagNode,
    Hunk,
    HunkClass,
    HunkSignals,
    NodeAnnotation,
    Tier,
)


def mk_hunk(
    file: str,
    new_start: int,
    added: list[str] | None = None,
    removed: list[str] | None = None,
    symbol: str | None = None,
    klass: HunkClass = HunkClass.BEHAVIORAL,
    new_count: int | None = None,
) -> Hunk:
    added = added or []
    removed = removed or []
    patch = "\n".join([f"-{l}" for l in removed] + [f"+{l}" for l in added])
    return Hunk(
        id=f"{file}:{new_start}",
        file=file,
        old_start=new_start,
        old_count=len(removed),
        new_start=new_start,
        new_count=new_count if new_count is not None else len(added),
        patch=patch,
        enclosing_symbol=symbol,
        klass=klass,
    )


def mk_sig(
    h: Hunk,
    defines: list[str] | None = None,
    uses: list[str] | None = None,
    score: float = 0.0,
    blast: int = 0,
    sensitive: list[str] | None = None,
    co_change: list[str] | None = None,
    test_touched: bool = False,
) -> HunkSignals:
    return HunkSignals(
        hunk_id=h.id,
        defines=defines or [],
        uses=uses or [],
        blast_radius=blast,
        sensitive=sensitive or [],
        co_change_miss=co_change or [],
        test_touched=test_touched,
        score=score,
    )


class TestDagBuild(unittest.TestCase):
    def test_empty_input(self) -> None:
        nodes, edges = dag.build([], {}, [])
        self.assertEqual(nodes, [])
        self.assertEqual(edges, [])

    def test_clusters_by_file_and_symbol(self) -> None:
        h1 = mk_hunk("a.py", 10, added=["x = 1"], symbol="foo")
        h2 = mk_hunk("a.py", 40, added=["y = 2"], symbol="foo")
        h3 = mk_hunk("a.py", 80, added=["z = 3"], symbol="bar")
        h4 = mk_hunk("b.py", 5, added=["w = 4"], symbol="foo")
        nodes, _ = dag.build([h1, h2, h3, h4], {}, [])
        self.assertEqual(len(nodes), 3)
        groups = sorted(tuple(sorted(n.hunk_ids)) for n in nodes)
        self.assertIn((h1.id, h2.id), groups)  # same file+symbol merged
        self.assertIn((h3.id,), groups)
        self.assertIn((h4.id,), groups)  # same symbol, other file: separate

    def test_mechanical_cluster_is_single_node_with_badge(self) -> None:
        h1 = mk_hunk("a.py", 1, added=["import x"], klass=HunkClass.MECHANICAL)
        h2 = mk_hunk("b.py", 1, added=["import x"], klass=HunkClass.MECHANICAL)
        h3 = mk_hunk("c.py", 9, added=["real change"], symbol="main")
        nodes, _ = dag.build([h1, h2, h3], {}, [[h1.id, h2.id]])
        self.assertEqual(len(nodes), 2)
        mech = next(n for n in nodes if set(n.hunk_ids) == {h1.id, h2.id})
        self.assertEqual(mech.badge, Badge.MECHANICAL_CHANGES)

    def test_edges_from_defuse_with_reason_and_no_self_edges(self) -> None:
        src = mk_hunk("buffer.py", 1, added=["class WriteBuffer:"], symbol="WriteBuffer")
        dst = mk_hunk("main.py", 20, added=["buf = WriteBuffer()"], symbol="main")
        signals = {
            # src also uses its own symbol: would be a self-edge, must be dropped
            src.id: mk_sig(src, defines=["WriteBuffer"], uses=["WriteBuffer"], score=9.0),
            dst.id: mk_sig(dst, uses=["WriteBuffer"], score=1.0),
        }
        nodes, edges = dag.build([src, dst], signals, [])
        self.assertEqual(edges, [DagEdge(src=1, dst=2, reason="WriteBuffer")])
        by_num = {n.number: n for n in nodes}
        self.assertEqual(by_num[1].hunk_ids, [src.id])  # definer is upstream
        self.assertEqual(by_num[1].badge, Badge.CODE_CHANGE)
        self.assertEqual(by_num[2].badge, Badge.CODE_CHANGE_EFFECTS)

    def test_cycle_broken_by_removing_edge_into_lower_score_node(self) -> None:
        a = mk_hunk("a.py", 1, added=["def a(): b()"], symbol="a")
        b = mk_hunk("b.py", 1, added=["def b(): a()"], symbol="b")
        signals = {
            a.id: mk_sig(a, defines=["a"], uses=["b"], score=9.0),
            b.id: mk_sig(b, defines=["b"], uses=["a"], score=1.0),
        }
        nodes, edges = dag.build([a, b], signals, [])
        # Cycle a<->b; the edge whose dst has the lower max score (a->b, dst
        # score 1.0) is removed, leaving b->a. b becomes the root.
        self.assertEqual(edges, [DagEdge(src=1, dst=2, reason="b")])
        by_num = {n.number: n for n in nodes}
        self.assertEqual(by_num[1].hunk_ids, [b.id])
        self.assertEqual(by_num[2].hunk_ids, [a.id])

    def test_numbering_topological_then_score_then_file_order(self) -> None:
        root = mk_hunk("core.py", 1, added=["def core(): pass"], symbol="core")
        low = mk_hunk("low.py", 1, added=["core()"], symbol="uses_low")
        high = mk_hunk("high.py", 1, added=["core()"], symbol="uses_high")
        signals = {
            root.id: mk_sig(root, defines=["core"], score=2.0),
            low.id: mk_sig(low, uses=["core"], score=3.0),
            high.id: mk_sig(high, uses=["core"], score=7.0),
        }
        nodes, _ = dag.build([root, low, high], signals, [])
        by_num = {n.number: n for n in nodes}
        self.assertEqual(by_num[1].hunk_ids, [root.id])   # topological first
        self.assertEqual(by_num[2].hunk_ids, [high.id])   # tie broken by score desc
        self.assertEqual(by_num[3].hunk_ids, [low.id])

    def test_numbering_score_tie_broken_by_file_order(self) -> None:
        first = mk_hunk("m1.py", 1, added=["x"], symbol="one")
        second = mk_hunk("m2.py", 1, added=["y"], symbol="two")
        nodes, _ = dag.build([first, second], {}, [])  # no signals: scores equal
        by_num = {n.number: n for n in nodes}
        self.assertEqual(by_num[1].hunk_ids, [first.id])
        self.assertEqual(by_num[2].hunk_ids, [second.id])

    def test_root_without_behavioral_hunks_is_effects(self) -> None:
        h = mk_hunk("style.py", 1, added=["# fmt"], klass=HunkClass.COSMETIC, symbol="f")
        nodes, _ = dag.build([h], {}, [])
        self.assertEqual(nodes[0].badge, Badge.CODE_CHANGE_EFFECTS)

    def test_title_uses_symbol_else_basename_plus_action(self) -> None:
        sym = mk_hunk("pkg/a.py", 1, added=["x"], symbol="WriteBuffer")
        add_only = mk_hunk("pkg/utils.py", 1, added=["x"])
        del_only = mk_hunk("pkg/gone.py", 1, removed=["x"], new_count=0)
        both = mk_hunk("pkg/mix.py", 1, added=["x"], removed=["y"])
        nodes, _ = dag.build([sym, add_only, del_only, both], {}, [])
        titles = {tuple(n.hunk_ids): n.title for n in nodes}
        self.assertEqual(titles[(sym.id,)], "WriteBuffer")
        self.assertEqual(titles[(add_only.id,)], "utils.py additions")
        self.assertEqual(titles[(del_only.id,)], "gone.py deletions")
        self.assertEqual(titles[(both.id,)], "mix.py edits")

    def test_mechanical_cluster_title_counts_similar_edits(self) -> None:
        h1 = mk_hunk("a.py", 3, added=["z"], klass=HunkClass.MECHANICAL)
        h2 = mk_hunk("b.py", 3, added=["z"], klass=HunkClass.MECHANICAL)
        nodes, _ = dag.build([h1, h2], {}, [[h1.id, h2.id]])
        self.assertEqual(nodes[0].title, "a.py 2 similar edits")

    def test_cluster_ids_missing_from_diff_are_ignored(self) -> None:
        h1 = mk_hunk("a.py", 1, added=["x"], symbol="f")
        nodes, edges = dag.build([h1], {}, [["ghost.py:1"], []])
        self.assertEqual(len(nodes), 1)
        self.assertEqual(edges, [])


def _plain_node(number: int, hunks: list[Hunk], badge: Badge = Badge.CODE_CHANGE) -> DagNode:
    return DagNode(number=number, title=f"node {number}", hunk_ids=[h.id for h in hunks],
                   badge=badge)


class TestTiersAssign(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = Config()
        self.ann = Annotation(summary="")

    def _one(self, hunk: Hunk, sig: HunkSignals | None = None,
             annotation: Annotation | None = None) -> "tiers.Item":
        node = _plain_node(1, [hunk])
        signals = {hunk.id: sig} if sig else {}
        items = tiers.assign([node], [hunk], signals, annotation or self.ann, self.cfg)
        self.assertEqual(len(items), 1)
        return items[0]

    def test_sensitive_floor_is_red(self) -> None:
        h = mk_hunk("auth/login.py", 5, added=["check(password)"])
        item = self._one(h, mk_sig(h, sensitive=["auth-path"]))
        self.assertEqual(item.tier, Tier.RED)
        self.assertIn("touches a sensitive area (auth-path)", item.chips)

    def test_behavioral_high_blast_is_red_low_blast_is_yellow(self) -> None:
        h = mk_hunk("core.py", 5, added=["def api(): ..."], symbol="api")
        self.assertEqual(self._one(h, mk_sig(h, blast=5)).tier, Tier.RED)
        self.assertEqual(self._one(h, mk_sig(h, blast=4)).tier, Tier.YELLOW)

    def test_high_blast_non_behavioral_is_not_red(self) -> None:
        h = mk_hunk("core.py", 5, added=["# comment"], klass=HunkClass.COSMETIC)
        self.assertEqual(self._one(h, mk_sig(h, blast=9)).tier, Tier.YELLOW)

    def test_workflow_file_is_red(self) -> None:
        h = mk_hunk(".github/workflows/ci.yml", 2, added=["run: make"],
                    klass=HunkClass.COSMETIC)
        self.assertEqual(self._one(h).tier, Tier.RED)

    def test_deleted_asserts_in_test_file_is_red(self) -> None:
        h = mk_hunk("tests/test_x.py", 9, removed=["assert x == 1", "assert y"],
                    added=["pass"], new_count=1)
        self.assertEqual(self._one(h).tier, Tier.RED)

    def test_moved_assert_in_test_file_is_not_weakening(self) -> None:
        h = mk_hunk("tests/test_x.py", 9, removed=["assert x == 1"],
                    added=["assert x == compute()"])
        self.assertEqual(self._one(h).tier, Tier.YELLOW)

    def test_added_skip_marker_in_test_file_is_red(self) -> None:
        h = mk_hunk("tests/test_x.py", 3, added=["@unittest.skip('later')"])
        self.assertEqual(self._one(h).tier, Tier.RED)

    def test_skip_marker_inside_a_string_literal_is_not_weakening(self) -> None:
        # A test ABOUT skip detection quotes the marker as fixture data. That
        # is data, not a skipped test — reading it as one forced Crux's own
        # test files red on a real PR (the detector detected itself).
        h = mk_hunk("tests/test_x.py", 3, added=[
            'h = mk_hunk("t.py", 3, added=["@unittest.skip(\'later\')"])',
            "marker = '@pytest.mark.skip'",
        ])
        self.assertEqual(self._one(h).tier, Tier.YELLOW)

    def test_deleted_assert_outside_test_file_is_not_weakening(self) -> None:
        h = mk_hunk("core.py", 3, removed=["assert invariant"], added=["pass"])
        self.assertEqual(self._one(h).tier, Tier.YELLOW)

    def test_mechanical_or_generated_zero_blast_is_green(self) -> None:
        m = mk_hunk("a.py", 1, added=["x"], klass=HunkClass.MECHANICAL)
        g = mk_hunk("uv.lock", 1, added=["x"], klass=HunkClass.GENERATED)
        node = _plain_node(1, [m, g], badge=Badge.MECHANICAL_CHANGES)
        items = tiers.assign([node], [m, g], {}, self.ann, self.cfg)
        self.assertEqual(items[0].tier, Tier.GREEN)

    def test_mechanical_with_blast_is_yellow(self) -> None:
        m = mk_hunk("a.py", 1, added=["x"], klass=HunkClass.MECHANICAL)
        self.assertEqual(self._one(m, mk_sig(m, blast=2)).tier, Tier.YELLOW)

    def test_design_decision_badge_and_minimum_yellow(self) -> None:
        m = mk_hunk("a.py", 1, added=["x"], klass=HunkClass.MECHANICAL)
        ann = Annotation(summary="", nodes={
            1: NodeAnnotation(number=1, title="5s flush window", why="tradeoff",
                              design_decision=True, minutes=2),
        })
        item = self._one(m, annotation=ann)  # floor would be GREEN
        self.assertEqual(item.badge, Badge.DESIGN_DECISION)
        self.assertEqual(item.tier, Tier.YELLOW)
        self.assertEqual(item.title, "5s flush window")

    def test_design_decision_does_not_lower_red(self) -> None:
        h = mk_hunk("auth/x.py", 1, added=["x"])
        ann = Annotation(summary="", nodes={
            1: NodeAnnotation(number=1, title="t", why="w", design_decision=True),
        })
        item = self._one(h, mk_sig(h, sensitive=["auth-path"]), annotation=ann)
        self.assertEqual(item.tier, Tier.RED)
        self.assertEqual(item.badge, Badge.DESIGN_DECISION)

    def test_change_the_model_left_out_never_lands_in_must_read(self) -> None:
        # D32: the prompt says omitting a change marks it routine. The rule
        # floor still keeps it ON the card — one line down, in the skim list,
        # flagged no_analysis so the card can show WHY it is there — but never
        # as a must-read with a machine-made title and nothing to say.
        h = mk_hunk("auth/x.py", 1, added=["check(password)"])
        ann = Annotation(summary="", nodes={
            1: NodeAnnotation(number=1, title="x.py additions", why="",
                              omitted_by_model=True),
        })
        item = self._one(h, mk_sig(h, sensitive=["auth-path"]), annotation=ann)
        self.assertEqual(item.tier, Tier.YELLOW)
        self.assertTrue(item.no_analysis)
        self.assertIn("touches a sensitive area (auth-path)", item.chips)

    def test_written_up_change_keeps_its_red_floor(self) -> None:
        h = mk_hunk("auth/x.py", 1, added=["check(password)"])
        ann = Annotation(summary="", nodes={
            1: NodeAnnotation(number=1, title="Login now checks the password",
                              why="w"),
        })
        item = self._one(h, mk_sig(h, sensitive=["auth-path"]), annotation=ann)
        self.assertEqual(item.tier, Tier.RED)
        self.assertFalse(item.no_analysis)

    def test_no_llm_run_keeps_every_floor(self) -> None:
        # --no-llm builds annotations from the change map itself: no model ran,
        # so nothing was omitted and the floors carry the whole card.
        h = mk_hunk("auth/x.py", 1, added=["check(password)"])
        ann = Annotation(summary="", nodes={
            1: NodeAnnotation(number=1, title="node 1", why=""),
        })
        item = self._one(h, mk_sig(h, sensitive=["auth-path"]), annotation=ann)
        self.assertEqual(item.tier, Tier.RED)

    def test_line_span_from_primary_file_only(self) -> None:
        h1 = mk_hunk("a.py", 10, added=["x"] * 5, new_count=5)
        h2 = mk_hunk("a.py", 40, added=["y"] * 3, new_count=3)
        other = mk_hunk("b.py", 500, added=["z"])
        node = _plain_node(1, [h1, h2, other])
        items = tiers.assign([node], [h1, h2, other], {}, self.ann, self.cfg)
        self.assertEqual(items[0].file, "a.py")
        self.assertEqual(items[0].line_start, 10)
        self.assertEqual(items[0].line_end, 42)  # 40 + 3 - 1, b.py ignored

    def test_pure_deletion_hunk_has_one_line_span(self) -> None:
        h = mk_hunk("a.py", 7, removed=["x"], new_count=0)
        item = self._one(h)
        self.assertEqual((item.line_start, item.line_end), (7, 7))

    def test_minutes_from_annotation_else_heuristic(self) -> None:
        h = mk_hunk("a.py", 1, added=["x"] * 88)
        ann = Annotation(summary="", nodes={
            1: NodeAnnotation(number=1, title="t", why="w", minutes=4),
        })
        self.assertEqual(self._one(h, annotation=ann).minutes, 4)
        self.assertEqual(self._one(h).minutes, 6)  # ceil(88 / 15)
        tiny = mk_hunk("a.py", 1, added=["x"])
        self.assertEqual(self._one(tiny).minutes, 1)

    def test_chips_sources_dedup_and_cap(self) -> None:
        h = mk_hunk("auth/x.py", 1, added=["y"])
        sig = mk_sig(h, blast=12, sensitive=["auth-path", "auth-path"],
                     co_change=["auth/other.py"])
        ann = Annotation(
            summary="",
            claims=[Claim(id="C1", text="unsure about flush timing",
                          hunk_ids=[h.id], uncertain=True),
                    Claim(id="C2", text="certain claim", hunk_ids=[h.id])],
            nodes={1: NodeAnnotation(number=1, title="t", why="w",
                                     chips=["untested", "extra-1", "extra-2"])},
        )
        item = self._one(h, sig, annotation=ann)
        self.assertLessEqual(len(item.chips), 6)
        self.assertEqual(len(item.chips), len(set(item.chips)))
        # D15: deterministic chips are plain English, never machine syntax
        self.assertIn("touches a sensitive area (auth-path)", item.chips)
        self.assertIn("called from 12 places", item.chips)
        self.assertIn(
            "usually changes together with auth/other.py, which was not changed here",
            item.chips)
        self.assertIn("no tests cover this change", item.chips)
        self.assertIn('the AI author said it was unsure: "unsure about flush timing"',
                      item.chips)
        self.assertNotIn('the AI author said it was unsure: "certain claim"', item.chips)

    def test_untested_chip_only_for_behavioral_untouched(self) -> None:
        h = mk_hunk("a.py", 1, added=["x"])
        chip = "no tests cover this change"
        self.assertIn(chip, self._one(h).chips)
        self.assertNotIn(chip, self._one(h, mk_sig(h, test_touched=True)).chips)
        mech = mk_hunk("a.py", 1, added=["x"], klass=HunkClass.MECHANICAL)
        self.assertNotIn(chip, self._one(mech).chips)

    def test_singular_blast_chip(self) -> None:
        h = mk_hunk("a.py", 1, added=["x"])
        self.assertIn("called from 1 place", self._one(h, mk_sig(h, blast=1)).chips)

    def test_suggested_tier_raises_above_floor(self) -> None:
        h = mk_hunk("a.py", 1, added=["x"])  # floor: YELLOW
        ann = Annotation(summary="", nodes={
            1: NodeAnnotation(number=1, title="t", why="w", suggested_tier="red")})
        self.assertEqual(self._one(h, annotation=ann).tier, Tier.RED)

    def test_suggested_tier_never_lowers_below_floor(self) -> None:
        h = mk_hunk("auth/x.py", 1, added=["x"])  # floor: RED (sensitive)
        ann = Annotation(summary="", nodes={
            1: NodeAnnotation(number=1, title="t", why="w", suggested_tier="green")})
        item = self._one(h, mk_sig(h, sensitive=["auth-path"]), annotation=ann)
        self.assertEqual(item.tier, Tier.RED)

    def test_suggested_tier_garbage_ignored(self) -> None:
        h = mk_hunk("a.py", 1, added=["x"])
        ann = Annotation(summary="", nodes={
            1: NodeAnnotation(number=1, title="t", why="w", suggested_tier="purple")})
        self.assertEqual(self._one(h, annotation=ann).tier, Tier.YELLOW)

    def test_deleted_file_item_uses_old_side_lines(self) -> None:
        h = Hunk(id="gone.py:0", file="gone.py", old_start=1, old_count=4,
                 new_start=0, new_count=0, patch="@@ -1,4 +0,0 @@\n-a\n-b\n-c\n-d")
        item = self._one(h)
        self.assertTrue(item.deleted)
        self.assertEqual((item.line_start, item.line_end), (1, 4))

    def test_deletion_hunk_in_surviving_file_is_not_deleted(self) -> None:
        h = mk_hunk("a.py", 7, removed=["x"], new_count=0)  # new_start=7 != 0
        self.assertFalse(self._one(h).deleted)

    def test_items_returned_in_dag_number_order(self) -> None:
        h1 = mk_hunk("a.py", 1, added=["x"])
        h2 = mk_hunk("b.py", 1, added=["y"])
        h3 = mk_hunk("c.py", 1, added=["z"])
        nodes = [_plain_node(3, [h3]), _plain_node(1, [h1]), _plain_node(2, [h2])]
        items = tiers.assign(nodes, [h1, h2, h3], {}, self.ann, self.cfg)
        self.assertEqual([i.number for i in items], [1, 2, 3])

    def test_annotation_none_is_tolerated(self) -> None:
        h = mk_hunk("a.py", 1, added=["x"])
        items = tiers.assign([_plain_node(1, [h])], [h], {}, None, self.cfg)
        self.assertEqual(items[0].tier, Tier.YELLOW)
        self.assertEqual(items[0].why, "")

    # -- D25: tests ride along — test-only items are capped at yellow --------

    def test_sensitive_keyword_in_test_file_capped_yellow(self) -> None:
        # e.g. a test faking subprocess calls must not go red for the keyword.
        h = mk_hunk("tests/test_post.py", 5, added=["mock subprocess call"])
        item = self._one(h, mk_sig(h, sensitive=["keyword:subprocess"]))
        self.assertEqual(item.tier, Tier.YELLOW)

    def test_high_blast_test_file_capped_yellow(self) -> None:
        h = mk_hunk("tests/test_x.py", 5, added=["def helper(): ..."], symbol="helper")
        self.assertEqual(self._one(h, mk_sig(h, blast=9)).tier, Tier.YELLOW)

    def test_llm_cannot_promote_test_only_to_red(self) -> None:
        h = mk_hunk("tests/test_x.py", 1, added=["assert x"])
        ann = Annotation(summary="", nodes={
            1: NodeAnnotation(number=1, title="t", why="w", suggested_tier="red")})
        self.assertEqual(self._one(h, annotation=ann).tier, Tier.YELLOW)

    def test_weakened_test_stays_red_despite_cap(self) -> None:
        h = mk_hunk("tests/test_x.py", 3, added=["@unittest.skip('later')"])
        ann = Annotation(summary="", nodes={
            1: NodeAnnotation(number=1, title="t", why="w", suggested_tier="red")})
        self.assertEqual(self._one(h, annotation=ann).tier, Tier.RED)

    def test_mixed_test_and_code_node_not_capped(self) -> None:
        code = mk_hunk("auth/x.py", 1, added=["check()"])
        test = mk_hunk("tests/test_x.py", 1, added=["assert check()"])
        node = _plain_node(1, [code, test])
        signals = {code.id: mk_sig(code, sensitive=["auth-path"])}
        items = tiers.assign([node], [code, test], signals, self.ann, self.cfg)
        self.assertEqual(items[0].tier, Tier.RED)

    def test_suffix_named_test_files_capped_yellow(self) -> None:
        # Test files that live NEXT TO the code (no tests/ dir): Go, JS/TS,
        # Ruby, Java. All must be recognized or sensitive keywords force red.
        for path in ("pkg/server_test.go", "src/api.test.ts",
                     "src/api.spec.ts", "spec/models/user_spec.rb",
                     "src/main/WidgetTest.java", "Sources/AppTests.swift",
                     "app/RequestSpec.java", "conftest.py"):
            with self.subTest(path=path):
                h = mk_hunk(path, 5, added=["mock subprocess call"])
                item = self._one(h, mk_sig(h, sensitive=["keyword:subprocess"]))
                self.assertEqual(item.tier, Tier.YELLOW, path)

    def test_non_test_lookalikes_not_capped(self) -> None:
        # "contest.js" / "inspect.py" are product code; sensitive stays red.
        for path in ("src/contest.js", "src/inspect.py", "src/latest.ts"):
            with self.subTest(path=path):
                h = mk_hunk(path, 5, added=["subprocess.run(cmd)"])
                item = self._one(h, mk_sig(h, sensitive=["keyword:subprocess"]))
                self.assertEqual(item.tier, Tier.RED, path)

    def test_assertions_moved_between_chunks_not_weakening(self) -> None:
        # A test rewrite: one chunk deletes 2 asserts, another adds 3. Net
        # coverage grew — counting per chunk used to force this red.
        h1 = mk_hunk("tests/test_x.py", 10,
                     removed=["assert a", "assert b"], added=["helper()"])
        h2 = mk_hunk("tests/test_x.py", 90,
                     added=["assert a2", "assert b2", "assert c2"])
        node = _plain_node(1, [h1, h2])
        items = tiers.assign([node], [h1, h2], {}, self.ann, self.cfg)
        self.assertEqual(items[0].tier, Tier.YELLOW)

    def test_net_assertion_loss_across_chunks_stays_red(self) -> None:
        h1 = mk_hunk("tests/test_x.py", 10,
                     removed=["assert a", "assert b", "assert c"])
        h2 = mk_hunk("tests/test_x.py", 90, added=["assert a2"])
        node = _plain_node(1, [h1, h2])
        items = tiers.assign([node], [h1, h2], {}, self.ann, self.cfg)
        self.assertEqual(items[0].tier, Tier.RED)

    # -- D23: >50-line items get a breakdown ---------------------------------

    def test_big_span_gets_fallback_breakdown(self) -> None:
        h1 = mk_hunk("a.py", 10, added=["x"] * 30, symbol="alpha", new_count=30)
        h2 = mk_hunk("a.py", 200, added=["y"] * 20, symbol="beta", new_count=20)
        node = _plain_node(1, [h1, h2])
        items = tiers.assign([node], [h1, h2], {}, self.ann, self.cfg)
        item = items[0]
        self.assertEqual((item.line_start, item.line_end), (10, 219))  # span 210
        self.assertEqual(item.breakdown,
                         ["the `alpha` part — `a.py:10-39`",
                          "the `beta` part — `a.py:200-219`"])

    def test_two_chunks_in_one_symbol_are_named_apart(self) -> None:
        # Both would otherwise render "the `alpha` part", which reads like a
        # duplicated line instead of a second place to look.
        h1 = mk_hunk("a.py", 10, added=["x"] * 30, symbol="alpha", new_count=30)
        h2 = mk_hunk("a.py", 200, added=["y"] * 20, symbol="alpha", new_count=20)
        node = _plain_node(1, [h1, h2])
        item = tiers.assign([node], [h1, h2], {}, self.ann, self.cfg)[0]
        self.assertEqual(item.breakdown,
                         ["the `alpha` part — `a.py:10-39`",
                          "another part of `alpha` — `a.py:200-219`"])

    def test_unnamed_chunks_are_named_apart(self) -> None:
        h1 = mk_hunk("a.py", 10, added=["x"] * 30, new_count=30)
        h2 = mk_hunk("a.py", 200, added=["y"] * 20, new_count=20)
        node = _plain_node(1, [h1, h2])
        item = tiers.assign([node], [h1, h2], {}, self.ann, self.cfg)[0]
        self.assertEqual([b.split(" — ")[0] for b in item.breakdown],
                         ["one changed chunk", "another changed chunk"])

    def test_small_span_gets_no_fallback_breakdown(self) -> None:
        h = mk_hunk("a.py", 10, added=["x"] * 30, new_count=30)
        self.assertEqual(self._one(h).breakdown, [])

    def test_single_huge_chunk_gets_one_window_plus_summary(self) -> None:
        # NOT tiled into 50-line windows: one entry pointer, one summary line.
        h = mk_hunk("a.py", 1, added=["x"] * 120, new_count=120)
        item = self._one(h)
        self.assertEqual(len(item.breakdown), 2)
        self.assertIn("`a.py:1-50`", item.breakdown[0])
        self.assertIn("remaining ~70 lines", item.breakdown[1])
        self.assertNotIn("`a.py:51", " ".join(item.breakdown))

    def test_fallback_reading_budget_is_bounded(self) -> None:
        # 600 changed lines across many chunks: at most 3 pointed-at windows
        # (~150 lines of reading) + one pointer-less summary for the rest.
        hunks = [mk_hunk("a.py", 1 + i * 100, added=["x"] * 75, new_count=75,
                         symbol=f"part{i}") for i in range(8)]
        node = _plain_node(1, hunks)
        items = tiers.assign([node], hunks, {}, self.ann, self.cfg)
        breakdown = items[0].breakdown
        self.assertEqual(len(breakdown), 4)  # 3 windows + summary
        pointed = sum(1 for b in breakdown if "`a.py:" in b)
        self.assertEqual(pointed, 3)
        self.assertIn("look routine to Crux", breakdown[-1])

    def test_fallback_picks_the_highest_scoring_chunks_first(self) -> None:
        dull = mk_hunk("a.py", 1, added=["x"] * 80, new_count=80, symbol="dull")
        hot = mk_hunk("a.py", 300, added=["y"] * 20, new_count=20, symbol="hot")
        node = _plain_node(1, [dull, hot])
        signals = {hot.id: mk_sig(hot, score=9.0),
                   dull.id: mk_sig(dull, score=0.1)}
        items = tiers.assign([node], [dull, hot], signals, self.ann, self.cfg)
        # the small-but-important chunk outranks the big dull one
        self.assertIn("`hot`", items[0].breakdown[0])
        self.assertIn("`a.py:300-319`", items[0].breakdown[0])

    def test_llm_breakdown_wins_over_fallback(self) -> None:
        h = mk_hunk("a.py", 1, added=["x"] * 120, new_count=120)
        ann = Annotation(summary="", nodes={
            1: NodeAnnotation(number=1, title="t", why="w",
                              breakdown=["the buffer itself — `a.py:1-45`",
                                         "the flush logic — `a.py:46-90`"])})
        item = self._one(h, annotation=ann)
        self.assertEqual(item.breakdown,
                         ["the buffer itself — `a.py:1-45`",
                          "the flush logic — `a.py:46-90`"])

    def test_deleted_file_gets_no_fallback_breakdown(self) -> None:
        h = Hunk(id="gone.py:0", file="gone.py", old_start=1, old_count=90,
                 new_start=0, new_count=0,
                 patch="@@ -1,90 +0,0 @@\n" + "\n".join(f"-l{i}" for i in range(90)))
        item = self._one(h)
        self.assertTrue(item.deleted)
        self.assertEqual(item.breakdown, [])

    # -- D24: before/now pass through onto the item --------------------------

    def test_before_after_passthrough(self) -> None:
        h = mk_hunk("a.py", 1, added=["x"])
        ann = Annotation(summary="", nodes={
            1: NodeAnnotation(number=1, title="t", why="w",
                              before="writes went straight to the database",
                              after="writes queue in a buffer first")})
        item = self._one(h, annotation=ann)
        self.assertEqual(item.before, "writes went straight to the database")
        self.assertEqual(item.after, "writes queue in a buffer first")

    def test_before_after_empty_without_annotation(self) -> None:
        h = mk_hunk("a.py", 1, added=["x"])
        item = self._one(h)
        self.assertEqual((item.before, item.after), ("", ""))

    def test_dag_output_feeds_tiers_end_to_end(self) -> None:
        src = mk_hunk("buffer.py", 1, added=["class WriteBuffer:"] * 20,
                      symbol="WriteBuffer", new_count=20)
        dst = mk_hunk("main.py", 20, added=["buf = WriteBuffer()"], symbol="main")
        signals = {
            src.id: mk_sig(src, defines=["WriteBuffer"], score=9.0, blast=12),
            dst.id: mk_sig(dst, uses=["WriteBuffer"], score=1.0),
        }
        nodes, edges = dag.build([src, dst], signals, [])
        items = tiers.assign(nodes, [src, dst], signals, self.ann, self.cfg)
        self.assertEqual([i.number for i in items], [1, 2])
        self.assertEqual(items[0].tier, Tier.RED)  # behavioral + blast 12
        self.assertEqual(items[0].file, "buffer.py")
        self.assertEqual(edges[0].reason, "WriteBuffer")


if __name__ == "__main__":
    unittest.main()
