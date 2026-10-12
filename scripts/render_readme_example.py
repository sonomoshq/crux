#!/usr/bin/env python3
# Copyright © 2026 Sonomos, Inc.
# SPDX-License-Identifier: Apache-2.0

"""Rebuild the README's illustrative card and navigation assets, without AI or API calls.

Run from a source checkout: python scripts/render_readme_example.py
The Markdown uses the real renderer with hand-authored annotations. The SVG is a
compact visual excerpt from that same fixture, not a screenshot of a live review.
"""
from __future__ import annotations

import html
from pathlib import Path
import sys
import textwrap

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from crux.models import Annotation, Badge, Config, Item, RepoInfo, Tier
from crux.render import render_card

SOURCE_SHA = "8298583df7cf2c3508fa998d65c4018261802730"
ASSETS = ROOT / "docs" / "assets"
EXAMPLES = ROOT / "docs" / "examples"


def scene() -> tuple[RepoInfo, list[Item], Annotation]:
    info = RepoInfo(
        root=str(ROOT), branch="example/review-coordination",
        owner="sonomoshq", repo="crux", head_sha=SOURCE_SHA,
        base_sha=SOURCE_SHA,
    )
    items = [
        Item(
            number=1, tier=Tier.RED, badge=Badge.CODE_CHANGE,
            title="Let newer push requests supersede older ones",
            file="crux/cli.py", line_start=427, line_end=439, minutes=1,
            why="Check for a newer request before and after waiting for the review lock.",
        ),
        Item(
            number=2, tier=Tier.RED, badge=Badge.CODE_CHANGE,
            title="Keep coordination local to a clone and branch",
            file="crux/superdebounce.py", line_start=85, line_end=88, minutes=1,
            why="The request key includes the checkout path and branch. Other machines have their own state.",
        ),
        Item(
            number=3, tier=Tier.YELLOW, badge=Badge.CODE_CHANGE,
            title="Run if the review lock file cannot be opened",
            file="crux/superdebounce.py", line_start=204, line_end=215, minutes=1,
        ),
    ]
    annotation = Annotation(
        summary="Coordinate repeated review requests from the same branch.",
        overview=[
            "Nearby push and PR-creation events share a review request key: `crux/cli.py:427-439`.",
            "Older pending requests stand down before the review begins.",
        ],
    )
    return info, items, annotation


def text(x: int, y: int, value: str, *, size: int = 20,
         color: str = "#1A1A1A", weight: int = 400,
         mono: bool = False) -> str:
    family = "monospace" if mono else "Arial, Helvetica, sans-serif"
    return (f'<text x="{x}" y="{y}" fill="{color}" font-family="{family}" '
            f'font-size="{size}" font-weight="{weight}">{html.escape(value)}</text>')


def visual(items: list[Item], annotation: Annotation) -> str:
    # Exact copy comes from the fixture; this is an explicitly labeled excerpt.
    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="1000" height="660" viewBox="0 0 1000 660" role="img" aria-labelledby="title desc">',
        '<title id="title">Illustrative Crux review card</title>',
        '<desc id="desc">A review-card excerpt using hand-written annotations about Crux code. It shows the big picture, two must-read changes, and one item worth a skim.</desc>',
        '<rect width="1000" height="660" rx="20" fill="#F3F7F4"/>',
        '<rect x="24" y="24" width="952" height="612" rx="14" fill="#FFFFFF" stroke="#D6DFD9"/>',
        text(56, 69, "Crux", size=27, weight=700),
        text(143, 68, SOURCE_SHA[:7], size=15, color="#526159", mono=True),
        '<rect x="668" y="42" width="276" height="30" rx="15" fill="#E6F0E9"/>',
        text(684, 62, "ILLUSTRATIVE REVIEW EXCERPT", size=12, weight=700, color="#365345"),
        text(56, 108, annotation.summary, size=22),
        '<path d="M56 132H944" stroke="#E5EBE7"/>',
        text(56, 172, "The big picture", size=22, weight=700),
        text(56, 203, "Nearby push and PR-creation events share a review request key.", size=19, color="#4D5A52"),
        text(56, 231, annotation.overview[1], size=19, color="#4D5A52"),
        '<circle cx="64" cy="271" r="6" fill="#BC413C"/>',
        text(82, 278, "Must read", size=22, weight=700),
    ]
    y = 320
    for it in items[:2]:
        parts.append(text(56, y, it.title, size=21, weight=700))
        for line in textwrap.wrap(it.why, width=88):
            y += 27
            parts.append(text(56, y, line, size=18, color="#4D5A52"))
        y += 27
        parts.append(text(56, y, f"{it.file}:{it.line_start}-{it.line_end}", size=15, color="#256C49", mono=True))
        y += 44
    parts += [
        '<circle cx="64" cy="542" r="6" fill="#B78321"/>',
        text(82, 549, "Worth a skim", size=22, weight=700),
        text(56, 583, items[2].title, size=20, weight=700),
        text(56, 608, f"{items[2].file}:{items[2].line_start}-{items[2].line_end}", size=15, color="#256C49", mono=True),
        '</svg>',
    ]
    return "\n".join(parts) + "\n"


def button(label: str, width: int, fill: str, ink: str) -> str:
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="36" viewBox="0 0 {width} 36" role="img" aria-label="{html.escape(label)}">
<title>{html.escape(label)}</title>
<rect x="0.5" y="0.5" width="{width-1}" height="35" rx="8" fill="{fill}" stroke="#789A86" stroke-opacity="0.35"/>
<text x="{width/2}" y="23" text-anchor="middle" font-family="Arial, Helvetica, sans-serif" font-size="13" font-weight="700" fill="{ink}">{html.escape(label)}</text>
</svg>
'''


def main() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    EXAMPLES.mkdir(parents=True, exist_ok=True)
    info, items, annotation = scene()
    # No fabricated diff size; the renderer's minimum read-time is an estimate.
    card = render_card(info, None, items, annotation, {}, cfg=Config(serve_port=0))
    lead = '''# Example review card

[← Back to the README](../../README.md#see-the-review-card)

This is an **illustrative example**, rendered by `crux.render.render_card` with
hand-written annotations about Crux's own review-coordination code. It is not an
AI-generated review, live PR screenshot, or benchmark. The read-time in the card
is the renderer's minimum estimate, not a measured review time.

The code links point to a real, fixed repository revision. No publishing or merge
actions are included in this example.

---

'''
    tail = '''

---

## Reproduce the example

From the Crux checkout:

```sh
python scripts/render_readme_example.py
```

This writes this Markdown example, a compact SVG excerpt, and the README's
navigation buttons. It uses the standard library and Crux's own renderer, with
no model calls, GitHub writes, or external credentials. The excerpt omits the
read-time header and renders the same hand-written annotations in a compact
layout; it is not a pixel-exact GitHub screenshot.
'''
    (EXAMPLES / "review-card.md").write_text(lead + card + tail, encoding="utf-8")
    (ASSETS / "crux-review-example.svg").write_text(visual(items, annotation), encoding="utf-8")
    for name, label, width, fill, ink in [
        ("get-started", "Get started", 126, "#CFE5D9", "#173D28"),
        ("see-example", "See a review card", 170, "#1A1A1A", "#FFFFFF"),
        ("contribute", "Contribute", 128, "#EAF1EC", "#173D28"),
    ]:
        (ASSETS / f"button-{name}.svg").write_text(button(label, width, fill, ink), encoding="utf-8")
    print("Wrote the review example and three README navigation buttons.")


if __name__ == "__main__":
    main()
