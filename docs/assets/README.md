# Crux social assets

Two minimal social cards for Crux, created on 2026-10-12. The artwork uses near-white space, near-black typography, and a small abstract code-line illustration with pastel mint highlights.

| Asset | Intended use | Dimensions | PNG size |
| --- | --- | --- | --- |
| [crux-social-wide.png](crux-social-wide.png) | Repository social preview, LinkedIn, and X | 1280 × 640 | 560,681 bytes |
| [crux-social-square.png](crux-social-square.png) | Instagram and other square placements | 1080 × 1080 | 701,154 bytes |

The wide PNG is below 1 MB. The square omits the supporting sentence to preserve space. Both cards include the credit to Sonomos co-founder Jack Rose.

## README interface assets

The README also uses [crux-review-example.svg](crux-review-example.svg) and three
local SVG navigation buttons. These are code-rendered interface assets so their
labels and source pointers remain exact. The card is an explicitly labeled
illustrative excerpt using hand-written annotations, with the full example
available in [review-card.md](../examples/review-card.md).

Rebuild those interface assets from the repository root with:

```sh
python scripts/render_readme_example.py
```

That command leaves the social PNGs untouched. Its only dependencies are the
Python standard library and Crux's own card renderer.

## Alt text

**Wide**

> Crux by Sonomos. Find the changes that matter. One clear starting point for every code review. Built at Sonomos by co-founder Jack Rose. github.com/sonomoshq/crux. A small abstract illustration shows gray code lines with a few mint highlights on a near-white background.

**Square**

> Crux by Sonomos. Find the changes that matter. Built at Sonomos by co-founder Jack Rose. github.com/sonomoshq/crux. Gray code lines with three mint highlights sit below the headline on a near-white background.

## Brand sources

Official source repository: [sonomoshq/Website](https://github.com/sonomoshq/Website), read at commit [7f2015c32d81ddef6406756efb77001c3b59b2ef](https://github.com/sonomoshq/Website/commit/7f2015c32d81ddef6406756efb77001c3b59b2ef).

| Source | What informed these cards |
| --- | --- |
| [scripts/assets/og-card.html](https://github.com/sonomoshq/Website/blob/7f2015c32d81ddef6406756efb77001c3b59b2ef/scripts/assets/og-card.html) | Current uppercase SONOMOS wordmark, solid bold geometric typography, generous spacing, white ground, and #1A1A1A ink. |
| [public/og-image.png](https://github.com/sonomoshq/Website/blob/7f2015c32d81ddef6406756efb77001c3b59b2ef/public/og-image.png) | The current social card was visually inspected as a brand reference. |
| [public/favicon.svg](https://github.com/sonomoshq/Website/blob/7f2015c32d81ddef6406756efb77001c3b59b2ef/public/favicon.svg) | The company traffic-light symbol was inspected to distinguish Sonomos branding. These Crux cards use only the uppercase wordmark. |
| [src/components/Logo.tsx](https://github.com/sonomoshq/Website/blob/7f2015c32d81ddef6406756efb77001c3b59b2ef/src/components/Logo.tsx) and [tailwind.config.ts](https://github.com/sonomoshq/Website/blob/7f2015c32d81ddef6406756efb77001c3b59b2ef/tailwind.config.ts) | Current header wordmark structure. The site's `font-futura` stack is Jost, Futura, Century Gothic, Arial, sans-serif. |
| [src/index.css](https://github.com/sonomoshq/Website/blob/7f2015c32d81ddef6406756efb77001c3b59b2ef/src/index.css) | Site palette: light background hsl(40 20% 97%), approximately #F9F8F6, and status green hsl(152 60% 38%), approximately #279B65. |
| [src/styles/fonts.css](https://github.com/sonomoshq/Website/blob/7f2015c32d81ddef6406756efb77001c3b59b2ef/src/styles/fonts.css) and [public/fonts-licenses.txt](https://github.com/sonomoshq/Website/blob/7f2015c32d81ddef6406756efb77001c3b59b2ef/public/fonts-licenses.txt) | Poppins body typography. The source website self-hosts Poppins and Jost under SIL OFL 1.1. |

Art direction requested Futura Bold for the SONOMOS wordmark and Poppins for all remaining text. The website does not bundle a Futura font file; its implemented wordmark uses Jost first. These images contain generated raster lettering, with no embedded or redistributed font binaries.

Color targets in the generation prompts:

| Role | Color |
| --- | --- |
| Background | #FAFBFA |
| Main ink | #1A1A1A |
| Quiet code lines | #DADFD9 |
| Pastel mint highlights | #CFE5D9 |

The pastel mint is a derived tint: 20% of the site's #279B65 status green over its #F9F8F6 light background. It is a prompt target, not a claim that the site declares a standalone token with that hex value.

## Production and validation

Created with OpenAI's built-in image generation tool in two independent calls. No reference image was passed to the generator; the inspected source informed the text prompts below. The composition is an abstract code-line illustration.

Original generated dimensions were 1774 × 887 for the wide card and 1254 × 1254 for the square. Final packaging used aspect-preserving Lanczos downsampling and optimized RGB PNG export. No cropping, retouching, layout changes, text overlays, or added graphics were applied after generation.

Both final files were opened and their dimensions checked. The text and visual composition were also inspected.

| Asset | SHA-256 |
| --- | --- |
| crux-social-wide.png | `c746ef401e13494356945f983e20146c3aec471acf61f1e6ec028fcb48b46b0d` |
| crux-social-square.png | `c003829d6ebdf0271e7d059351e21a767295929aa7ca4e28f17b1ae9875d03ea` |

## Exact generation prompt: wide

```text
Create a finished premium minimalist typographic social card for the open-source code-review project Crux. This is a new graphic, a clean flat editorial design on a uniform near-white #FAFBFA background. Exact aspect ratio 2:1, landscape, intended final export 1280 by 640 pixels. Edge-to-edge card, no device mockup, no surrounding canvas frame. Keep all artwork within safe margins of 72 px left/right and 52 px top/bottom at 1280x640.

Brand and typography: near-black #1A1A1A, subtle gray #DADFD9, and one pastel mint accent #CFE5D9. Render the Sonomos company name as the plain wordmark "SONOMOS" in Futura Bold, solid uppercase, subtly wide tracking. All other text uses Poppins, geometric sans serif, beautifully typeset and extremely crisp. The brand wordmark has no pictorial icon. No traffic-light icon, lock icon, shield, invented brand symbol, striped lettering, or product logo. No gradients, textures, shadows, glass, 3D effects, decorative badges, buttons, or interface frame.

Layout: elegant Swiss editorial spacing with very generous empty space. Top-left small prominent project name "Crux" in bold 36 px type; top-right a modest "SONOMOS" wordmark in 24 px type. Main headline occupies the left two-thirds, set large and dark at approximately 66 px, left aligned, two deliberate lines:
"Find the changes"
"that matter."
The first line and second line must read together as the exact sentence "Find the changes that matter."
Below the headline, one line of Poppins 24 px text, exact wording:
"One clear starting point for every code review."
In the right third, one restrained abstract illustration made only of six short rows of thin horizontal line segments suggesting code. Most segments pale gray; only three selected segments use pastel mint #CFE5D9 with a slightly darker thin accent. It is an abstract flat line drawing, no readable code, no numbers, no screenshot, no panel, no glow. It visually suggests finding a few meaningful lines within a larger change. Keep this illustration small and quiet so the headline is the focal point.

At the bottom-left, a single clear small Poppins credit at approximately 20 px, exact wording:
"Built at Sonomos by co-founder Jack Rose"
At the bottom-right, clear Poppins 20 px footer, exact wording:
"github.com/sonomoshq/crux"

Show only the exact texts specified. Preserve exact spelling, capitalization, apostrophes, hyphen, and periods. Each text appears once. Never include fake outputs, metrics, approval decisions, performance claims, or extra copy. Clean readable lettering at social thumbnail size; meticulous baseline alignment and optical spacing. The result should feel like a calm premium software company announcement designed by a top editorial graphic designer.
```

## Exact generation prompt: square

```text
Create a finished premium minimalist typographic Instagram social card for the open-source code-review project Crux. This is a new graphic, a clean flat editorial design on a uniform near-white #FAFBFA background. Exact aspect ratio 1:1, square, intended final export 1080 by 1080 pixels. Edge-to-edge card, no device mockup, no surrounding canvas frame. Keep every element within safe margins of 76 px.

Brand and typography: near-black #1A1A1A, subtle gray #DADFD9, and one pastel mint accent #CFE5D9. Render the Sonomos company name as the plain wordmark "SONOMOS" in Futura Bold, solid uppercase, subtly wide tracking. All other text uses Poppins, geometric sans serif, beautifully typeset and extremely crisp. The brand wordmark has no pictorial icon. No traffic-light icon, lock icon, shield, invented brand symbol, striped lettering, or product logo. No gradients, textures, shadows, glass, 3D effects, decorative badges, buttons, or interface frame.

Layout: an elegant editorial poster with generous empty space. Top-left a modest but prominent project name "Crux" in bold 42 px type; top-right a modest "SONOMOS" wordmark in 27 px type. Large headline in the upper-middle, starting around 235 px from the top, left aligned, dark, set at approximately 82 px, on two deliberate lines:
"Find the changes"
"that matter."
The first line and second line must read together as the exact sentence "Find the changes that matter."
Below the headline, one restrained abstract illustration made only of six short rows of thin horizontal line segments suggesting code, arranged as a quiet composition about 480 px wide. Most segments pale gray; only three selected segments use pastel mint #CFE5D9 with a slightly darker thin accent. It is an abstract flat line drawing, no readable code, no numbers, no screenshot, no panel, no glow. It visually suggests finding a few meaningful lines within a larger change. Keep this illustration airy and quiet so the headline is the focal point.

Near the lower-left, a single clear Poppins credit at approximately 24 px, exact wording:
"Built at Sonomos by co-founder Jack Rose"
Below it, clear Poppins 24 px footer, exact wording:
"github.com/sonomoshq/crux"
Omit the supporting sentence from this square card so the design has ample breathing room.

Show only the exact texts specified. Preserve exact spelling, capitalization, hyphen, and period. Each text appears once. Never include fake outputs, metrics, approval decisions, performance claims, or extra copy. Clean readable lettering at social thumbnail size; meticulous baseline alignment and optical spacing. The result should feel like a calm premium software company announcement designed by a top editorial graphic designer.
```
