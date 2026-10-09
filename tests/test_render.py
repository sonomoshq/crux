# Copyright © 2026 Sonomos, Inc.
# SPDX-License-Identifier: Apache-2.0

"""Golden-style tests for crux.render — assert structure, not exact whitespace.

The card is high-level first: a header with read-time, a `summary` line, the
big-picture `overview` (each idea linked to its code), the review's user-level
change map (D33), then tight Must-read / Worth-a-skim / Safe-to-skip sections.
No tier tables, claim audit, design notes, or collapsibles.
"""
from __future__ import annotations

import dataclasses
import hashlib
import tempfile
import unittest
from pathlib import Path

from crux import render
from crux.models import (
    CARD_MARKER,
    Annotation,
    Badge,
    ChangeMap,
    Config,
    Item,
    MapArrow,
    MapStep,
    RepoInfo,
    Tier,
    jargon_hits,
)

HEAD_SHA = "a3f9c12deadbeef0123456789abcdef012345678"


def make_info() -> RepoInfo:
    return RepoInfo(
        root="/repo",
        branch="feat/buffer",
        head_sha=HEAD_SHA,
        base_sha="b" * 40,
        owner="example-org",
        repo="crux",
    )


def make_item(number: int, tier: Tier, **kw) -> Item:
    defaults = dict(
        badge=Badge.CODE_CHANGE,
        title=f"item {number}",
        file="editor/autosave.py",
        line_start=1,
        line_end=88,
        minutes=2,
    )
    defaults.update(kw)
    return Item(number=number, tier=tier, **defaults)


def make_scene() -> tuple[list[Item], Annotation, dict]:
    items = [
        make_item(
            1,
            Tier.RED,
            title="new WriteBuffer class",
            minutes=6,
            why="Events pile up in memory and get written in one batch.",
        ),
        make_item(
            2,
            Tier.YELLOW,
            badge=Badge.CODE_CHANGE_EFFECTS,
            title="flush-on-shutdown | handler",
            file="editor/main.py",
            line_start=88,
            line_end=88,
        ),
        make_item(
            3,
            Tier.GREEN,
            badge=Badge.MECHANICAL_CHANGES,
            title="12 call sites db.insert to buffer.add",
            file="ingest/events.py",
            line_start=41,
            line_end=41,
        ),
    ]
    annotation = Annotation(
        summary="Batch draft saves.",
        overview=[
            "Writes are batched through a new buffer, cutting database calls — `editor/autosave.py:1-88`",
            "⚠️ A failed flush drops the batch instead of retrying — `editor/autosave.py:61`",
        ],
        change_map=ChangeMap(
            steps=[
                MapStep(id="collect", label="Editor takes a keystroke"),
                MapStep(id="wait", label='Waits in the "5s" buffer'),
                MapStep(id="write", label="Batch written to the database"),
            ],
            arrows=[
                MapArrow(src="collect", dst="wait"),
                MapArrow(src="wait", dst="write", label="every 5 seconds"),
            ],
        ),
    )
    gate_stats = {"total_lines": 812, "behavioral_lines": 300}
    return items, annotation, gate_stats


def render_scene(pr_number: int | None = 7, cfg: Config | None = None) -> str:
    items, annotation, stats = make_scene()
    return render.render_card(make_info(), pr_number, items, annotation, stats,
                              cfg=cfg)


def mermaid_block(card: str) -> str:
    start = card.index("```mermaid")
    end = card.index("```", start + 3)
    return card[start:end]


class TestLinks(unittest.TestCase):
    def test_permalink_range(self) -> None:
        url = render.permalink(make_info(), "editor/autosave.py", 1, 88)
        self.assertEqual(
            url,
            f"https://github.com/example-org/crux/blob/{HEAD_SHA}/editor/autosave.py#L1-L88",
        )

    def test_permalink_single_line(self) -> None:
        url = render.permalink(make_info(), "config/editor.toml", 12, 12)
        self.assertTrue(url.endswith("#L12"))
        self.assertNotIn("-L", url.rsplit("#", 1)[1])

    def test_diff_anchor_hashes_path(self) -> None:
        path = "editor/autosave.py"
        digest = hashlib.sha256(path.encode("utf-8")).hexdigest()
        url = render.diff_anchor(make_info(), 7, path, 34)
        self.assertEqual(
            url, f"https://github.com/example-org/crux/pull/7/files#diff-{digest}R34"
        )

    def test_permalink_percent_encodes_path(self) -> None:
        url = render.permalink(make_info(), "docs/read me.md", 3, 3)
        self.assertIn("/docs/read%20me.md#L3", url)
        self.assertNotIn(" ", url)

    def test_deleted_item_links_base_side_blob(self) -> None:
        items, annotation, stats = make_scene()
        items[0].deleted = True
        items[0].line_start, items[0].line_end = 1, 40
        card = render.render_card(make_info(), 7, items,
                                  annotation, stats)
        base_sha = "b" * 40
        self.assertIn(f"/blob/{base_sha}/editor/autosave.py#L1-L40", card)
        digest = hashlib.sha256(b"editor/autosave.py").hexdigest()
        self.assertIn(f"#diff-{digest}L1", card)  # base-side diff column


class TestCardHeader(unittest.TestCase):
    def test_marker_is_first_line(self) -> None:
        card = render_scene()
        self.assertEqual(card.splitlines()[0], CARD_MARKER)

    def test_header_has_short_sha_and_read_time(self) -> None:
        header = render_scene().splitlines()[1]
        self.assertTrue(header.startswith("## Crux"))
        self.assertIn("`a3f9c12`", header)
        # 812 changed lines / 120 ≈ 7 min, but the card is a <=5-minute brief
        # by contract: the header never claims more (field-flagged at ~8).
        self.assertIn("~5 min read", header)

    def test_summary_rendered_in_italics(self) -> None:
        self.assertIn("_Batch draft saves._", render_scene())


class TestBigPicture(unittest.TestCase):
    def test_overview_leads_the_body(self) -> None:
        card = render_scene()
        self.assertIn("### The big picture", card)
        # both ideas present, before the change map
        self.assertLess(card.index("### The big picture"), card.index("### Change map"))
        self.assertIn("Writes are batched through a new buffer", card)
        self.assertIn("⚠️ A failed flush drops the batch", card)

    def test_code_pointer_is_linkified(self) -> None:
        card = render_scene()
        # `editor/autosave.py:61` becomes a blob link to that line
        self.assertIn(
            f"[`editor/autosave.py:61`](https://github.com/example-org/crux/blob/{HEAD_SHA}"
            f"/editor/autosave.py#L61)",
            card,
        )
        # a range pointer keeps the range in both label and anchor
        self.assertIn("/editor/autosave.py#L1-L88)", card)

    def test_no_big_picture_when_overview_empty(self) -> None:
        items, _ann, stats = make_scene()
        card = render.render_card(make_info(), 7, items,
                                  Annotation(summary=""), stats)
        self.assertNotIn("### The big picture", card)


class TestChangeMap(unittest.TestCase):
    """D33: the map is the review's own user-level flow of what happens — not
    the code graph, which never reaches the reviewer as a diagram."""

    def _card(self, cmap: ChangeMap | None) -> str:
        items, annotation, stats = make_scene()
        annotation.change_map = cmap
        return render.render_card(make_info(), 7, items, annotation, stats)

    def test_graph_lr_steps_and_arrows(self) -> None:
        block = mermaid_block(render_scene())
        self.assertIn("graph LR", block)
        self.assertIn('n1["Editor takes a keystroke"]', block)
        self.assertIn("n1 --> n2", block)

    def test_arrow_label_rendered_when_given(self) -> None:
        self.assertIn("n2 -->|every 5 seconds| n3", mermaid_block(render_scene()))

    def test_no_item_numbers_or_badges_inside_diagram(self) -> None:
        block = mermaid_block(render_scene())
        for badge in Badge:
            self.assertNotIn(badge.value, block)
        # numbering belongs to the review lists; the map is plain English only
        self.assertNotIn("1 · ", block)

    def test_double_quotes_in_labels_escaped(self) -> None:
        block = mermaid_block(render_scene())
        self.assertNotIn('"5s"', block)
        self.assertIn("#quot;5s#quot;", block)

    def test_step_ids_are_renumbered_never_echoed(self) -> None:
        # whatever the model called its steps stays out of the diagram source
        block = mermaid_block(self._card(ChangeMap(
            steps=[MapStep(id='push "a" branch', label="Developer pushes a branch"),
                   MapStep(id="post", label="Review posted on the PR")],
            arrows=[MapArrow(src='push "a" branch', dst="post")])))
        self.assertNotIn('push "a" branch', block)
        self.assertIn('n1["Developer pushes a branch"]', block)
        self.assertIn("n1 --> n2", block)

    def test_no_map_when_the_review_sent_none(self) -> None:
        self.assertNotIn("```mermaid", self._card(None))

    def test_no_map_for_loose_boxes(self) -> None:
        # steps with nothing connecting them are a bullet list, not a picture
        self.assertNotIn("```mermaid", self._card(ChangeMap(
            steps=[MapStep(id="a", label="One thing happens"),
                   MapStep(id="b", label="Another thing happens")],
            arrows=[])))

    def test_no_map_for_a_single_step(self) -> None:
        self.assertNotIn("```mermaid", self._card(ChangeMap(
            steps=[MapStep(id="a", label="One thing happens")],
            arrows=[MapArrow(src="a", dst="a")])))


class TestMustRead(unittest.TestCase):
    def test_red_item_line_and_why(self) -> None:
        card = render_scene()
        self.assertIn("### 🔴 Must read", card)
        # D32: a plain code change carries no tag — the title leads the line.
        self.assertIn("- **new WriteBuffer class** ·", card)
        self.assertNotIn("🔧 code", card)
        self.assertIn("Events pile up in memory", card)

    def test_design_decision_badge_shown(self) -> None:
        items, annotation, stats = make_scene()
        items[0].badge = Badge.DESIGN_DECISION
        card = render.render_card(make_info(), 7, items,
                                  annotation, stats)
        self.assertIn("🎯 design decision · **new WriteBuffer class**", card)

    def test_informative_badges_survive(self) -> None:
        # D32 drops only the no-op "code" tag; the three that say something the
        # title does not are still shown.
        items, annotation, stats = make_scene()
        for badge, tag in ((Badge.CODE_CHANGE_EFFECTS, "↳ ripple effect"),
                           (Badge.MECHANICAL_CHANGES, "⚙️ mechanical"),
                           (Badge.DESIGN_DECISION, "🎯 design decision")):
            items[0].badge = badge
            card = render.render_card(make_info(), 7, items,
                                      annotation, stats)
            self.assertIn(f"- {tag} · **new WriteBuffer class** ·", card)

    def test_no_checkbox_questions(self) -> None:
        self.assertNotIn("- [ ]", render_scene())

    def test_diff_link_present_with_pr(self) -> None:
        self.assertIn("[diff]", render_scene(pr_number=7))

    def test_diff_link_omitted_without_pr(self) -> None:
        self.assertNotIn("[diff]", render_scene(pr_number=None))


class TestBeforeNow(unittest.TestCase):
    """D24: Crux states the comparison itself — never 'compare the diff'."""

    def test_before_now_line_rendered(self) -> None:
        items, annotation, stats = make_scene()
        items[0].before = "writes went straight to the database"
        items[0].after = "writes queue in a buffer first"
        card = render.render_card(make_info(), 7, items,
                                  annotation, stats)
        self.assertIn(
            "  **Before:** writes went straight to the database "
            "**Now:** writes queue in a buffer first", card)

    def test_no_line_when_either_side_missing(self) -> None:
        items, annotation, stats = make_scene()
        items[0].before = "only the before side"
        card = render.render_card(make_info(), 7, items,
                                  annotation, stats)
        self.assertNotIn("**Before:**", card)


class TestBreakdown(unittest.TestCase):
    """D23: an item spanning more than max_read_lines shows its breakdown as
    sub-bullets, each with its own <=50-line link — never one big code read."""

    def _card(self, **item_kw) -> str:
        items, annotation, stats = make_scene()
        for k, v in item_kw.items():
            setattr(items[0], k, v)
        return render.render_card(make_info(), 7, items,
                                  annotation, stats)

    def test_breakdown_rendered_for_big_span(self) -> None:
        card = self._card(
            line_start=1, line_end=200,
            breakdown=["the buffer class itself — `editor/autosave.py:1-48`",
                       "the flush logic — `editor/autosave.py:49-96`"])
        self.assertIn("  - the buffer class itself — ", card)
        self.assertIn(f"/editor/autosave.py#L1-L48)", card)
        self.assertIn(f"/editor/autosave.py#L49-L96)", card)

    def test_breakdown_hidden_for_small_span(self) -> None:
        card = self._card(
            line_start=1, line_end=40,
            breakdown=["needless part — `editor/autosave.py:1-20`"])
        self.assertNotIn("needless part", card)

    def test_breakdown_hidden_for_deleted_item(self) -> None:
        card = self._card(
            deleted=True, line_start=1, line_end=200,
            breakdown=["gone part — `editor/autosave.py:1-50`"])
        self.assertNotIn("gone part", card)

    def test_yellow_never_renders_a_breakdown(self) -> None:
        # D30: skim entries are strictly one line — sub-bullets on yellows
        # were a top length driver of the field-flagged ~8-minute cards.
        items, annotation, stats = make_scene()
        items[1].line_start, items[1].line_end = 10, 180
        items[1].breakdown = ["the shutdown wiring — `editor/main.py:88-120`"]
        card = render.render_card(make_info(), 7, items,
                                  annotation, stats)
        self.assertNotIn("the shutdown wiring", card)

    def test_custom_cap_from_config(self) -> None:
        # with a large cap, a 200-line span needs no breakdown
        items, annotation, stats = make_scene()
        items[0].line_start, items[0].line_end = 1, 200
        items[0].breakdown = ["some part — `editor/autosave.py:1-48`"]
        card = render.render_card(make_info(), 7, items,
                                  annotation, stats, cfg=Config(max_read_lines=300))
        self.assertNotIn("some part", card)


class TestFiveMinuteCard(unittest.TestCase):
    """D30: the card is a <=5-minute brief — must-read shows at most 5 items,
    with the overflow accounted for in one closing line."""

    def test_must_read_capped_at_five(self) -> None:
        items, annotation, stats = make_scene()
        red = items[0]
        many = [dataclasses.replace(red, number=n) for n in range(1, 8)]
        card = render.render_card(make_info(), 7, many + items[1:],
                                  annotation, stats)
        self.assertIn("…and 2 more — see the diff", card)
        # exactly 5 rendered red bullets, not 7
        must_read = card.split("### 🔴 Must read")[1].split("###")[0]
        self.assertEqual(
            sum(1 for l in must_read.splitlines() if l.startswith("- ")
                and "…and" not in l), 5)


class TestSkim(unittest.TestCase):
    def test_yellow_is_a_plain_bullet(self) -> None:
        card = render_scene()
        self.assertIn("### 🟡 Worth a skim", card)
        self.assertIn("↳ ripple effect · **flush-on-shutdown | handler** ·", card)
        # no markdown table anymore
        self.assertNotIn("| # | What it is | Where |", card)

    def _skim_line(self, card: str) -> str:
        section = card.split("### 🟡 Worth a skim")[1].split("###")[0]
        return next(l for l in section.splitlines() if l.startswith("- "))

    def test_unwritten_item_shows_the_rule_that_flagged_it(self) -> None:
        # D32: nobody wrote about this change, so its title is machine-made
        # ("main.py edits"). The line says why it is on the card at all.
        items, annotation, stats = make_scene()
        items[1].title = "main.py edits"
        items[1].no_analysis = True
        items[1].chips = ["touches a sensitive area (auth)", "called from 3 places"]
        line = self._skim_line(render.render_card(make_info(), 7, items,
                                                  annotation, stats))
        self.assertIn("**main.py edits** · touches a sensitive area (auth) ·", line)
        # one reason only — the skim entry stays a single tight line (D30)
        self.assertNotIn("called from 3 places", line)

    def test_written_items_come_before_machine_flagged_ones(self) -> None:
        # D32: the capped skim slots go to what the review wrote about first.
        items, annotation, stats = make_scene()
        flagged = dataclasses.replace(items[1], number=4, title="post.py edits",
                                      no_analysis=True)
        card = render.render_card(make_info(), 7, [items[0], flagged, items[1],
                                                   items[2]],
                                  annotation, stats)
        section = card.split("### 🟡 Worth a skim")[1]
        self.assertLess(section.index("flush-on-shutdown"),
                        section.index("post.py edits"))

    def test_written_item_keeps_its_line_clean(self) -> None:
        items, annotation, stats = make_scene()
        items[1].chips = ["called from 3 places"]
        line = self._skim_line(render.render_card(make_info(), 7, items,
                                                  annotation, stats))
        self.assertNotIn("called from 3 places", line)


class TestSafeToSkip(unittest.TestCase):
    def test_single_tally_line(self) -> None:
        card = render_scene()
        self.assertIn("### 🟢 Safe to skip", card)
        self.assertIn("1 line across 1 group, machine-checked", card)
        # no per-line green bullets or collapsible details
        self.assertNotIn("<details>", card)


class TestTruncation(unittest.TestCase):
    def test_over_cap_is_truncated_with_note(self) -> None:
        card = render_scene(cfg=Config(comment_char_cap=120))
        self.assertLessEqual(len(card), 120 + len("\n\n_(truncated)_"))
        self.assertTrue(card.endswith("_(truncated)_"))


class TestIntegrationTestComment(unittest.TestCase):
    def test_renders_numbered_steps_with_marker(self) -> None:
        from crux.models import TEST_MARKER
        body = render.render_test_comment(
            make_info(),
            ["Start the app: `crux run`", "Push a branch and confirm PR #N opens"])
        self.assertTrue(body.startswith(TEST_MARKER))
        self.assertIn("## 🧪 How to verify this", body)
        self.assertIn("1. Start the app: `crux run`", body)
        self.assertIn("2. Push a branch", body)

    def test_links_file_references_in_steps(self) -> None:
        body = render.render_test_comment(
            make_info(), ["Open `editor/autosave.py:61` and set a breakpoint"])
        self.assertIn(f"/editor/autosave.py#L61)", body)


class TestVerifyComment(unittest.TestCase):
    def _info(self, root: str) -> RepoInfo:
        return RepoInfo(root=root, branch="feat", head_sha=HEAD_SHA,
                        base_sha="b" * 40, owner="example-org", repo="crux")

    def test_prerequisites_link_to_readme_install_section(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "README.md").write_text(
                "# Proj\n\n## Install\n\nrun the installer.\n", encoding="utf-8")
            comment = render.render_test_comment(self._info(d), ["do the thing"])
        self.assertIn("**Prerequisites:**", comment)
        self.assertIn("[Install]", comment)
        self.assertIn(f"blob/{HEAD_SHA}/README.md#install", comment)
        self.assertIn("1. do the thing", comment)

    def test_readme_without_install_heading_links_the_file(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "README.md").write_text("# Proj\n\njust prose.\n",
                                               encoding="utf-8")
            comment = render.render_test_comment(self._info(d), ["step"])
        self.assertIn("[README.md]", comment)
        self.assertIn(f"blob/{HEAD_SHA}/README.md", comment)
        self.assertNotIn("README.md#", comment)  # no section anchor

    def test_no_setup_doc_omits_prerequisites(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            comment = render.render_test_comment(self._info(d), ["step"])
        self.assertNotIn("Prerequisites", comment)
        self.assertIn("1. step", comment)


class TestFailureAndSkip(unittest.TestCase):
    def test_failure_card_is_short_and_loud(self) -> None:
        card = render.render_failure_card(make_info(), RuntimeError("gh exited 1: bad token"))
        lines = card.splitlines()
        self.assertEqual(lines[0], CARD_MARKER)
        self.assertIn("`a3f9c12`", lines[1])
        self.assertIn("FAILED", lines[1])
        self.assertIn("gh exited 1: bad token", card)
        self.assertLess(len(lines), 8)

    def test_failure_card_accepts_str_and_multiline(self) -> None:
        card = render.render_failure_card(make_info(), "boom\ntraceback junk")
        self.assertIn("boom", card)
        self.assertNotIn("traceback junk", card)

    def test_skip_note_includes_stats(self) -> None:
        note = render.render_skip_note({"behavioral_lines": 12, "total_lines": 40})
        self.assertIn("skip", note)
        self.assertIn("behavioral_lines=12", note)
        self.assertIn("total_lines=40", note)
        self.assertNotIn("\n", note)

    def test_skip_note_empty_stats(self) -> None:
        self.assertIn("skip", render.render_skip_note({}))


class TestPlainEnglishD15(unittest.TestCase):
    def test_full_card_contains_no_banned_jargon(self):
        card = render_scene()
        self.assertEqual(jargon_hits(card), [])


if __name__ == "__main__":
    unittest.main()
