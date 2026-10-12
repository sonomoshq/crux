# Example review card

[← Back to the README](../../README.md#see-the-review-card)

This is an **illustrative example**, rendered by `crux.render.render_card` with
hand-written annotations about Crux's own review-coordination code. It is not an
AI-generated review, live PR screenshot, or benchmark. The read-time in the card
is the renderer's minimum estimate, not a measured review time.

The code links point to a real, fixed repository revision. No publishing or merge
actions are included in this example.

---

<!-- crux:card -->
## Crux · `8298583` · ~1 min read
_Coordinate repeated review requests from the same branch._

### The big picture
- Nearby push and PR-creation events share a review request key: [`crux/cli.py:427-439`](https://github.com/sonomoshq/crux/blob/8298583df7cf2c3508fa998d65c4018261802730/crux/cli.py#L427-L439).
- Older pending requests stand down before the review begins.

### 🔴 Must read
- **Let newer push requests supersede older ones** · [crux/cli.py:427-439](https://github.com/sonomoshq/crux/blob/8298583df7cf2c3508fa998d65c4018261802730/crux/cli.py#L427-L439)
  Check for a newer request before and after waiting for the review lock.
- **Keep coordination local to a clone and branch** · [crux/superdebounce.py:85-88](https://github.com/sonomoshq/crux/blob/8298583df7cf2c3508fa998d65c4018261802730/crux/superdebounce.py#L85-L88)
  The request key includes the checkout path and branch. Other machines have their own state.

### 🟡 Worth a skim
- **Run if the review lock file cannot be opened** · [crux/superdebounce.py:204-215](https://github.com/sonomoshq/crux/blob/8298583df7cf2c3508fa998d65c4018261802730/crux/superdebounce.py#L204-L215)

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
