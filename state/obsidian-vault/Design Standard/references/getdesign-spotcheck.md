# getdesign.md 4-brand spot-check

**Purpose:** Empirical validation for `~/design-standard/DESIGN.md`. Fetched DESIGN.md files from getdesign.md for four Apple-adjacent brands on 2026-04-12 and extracted per-brand tokens. Used to confirm the standard's locked decisions against real reference systems.

**URL pattern:** `https://getdesign.md/design-md/<slug>/DESIGN.md` (Linear uses `linear.app` as the domain-based slug).

**Brands fetched:** Apple, Vercel, Linear, Raycast. **All 4 successful.**

---

## Per-brand token table

| Brand | Font | Display size/weight | Tracking | Base body | Accent | Neutral | Spacing | Radius | Motion |
|---|---|---|---|---|---|---|---|---|---|
| **Apple** | SF Pro Display/Text (optical) | 56px / 600 | -0.28px | 17px / 400, LH 1.47 | Apple Blue `#0071e3` (single) | Cool binary: `#000` / `#f5f5f7` / `#1d1d1f` (near-black) | 8px base, micro 2-11px | 5/8/11/12/**980**(pill)/50% | Subtle scale(0.9) on active, glass blur `saturate(180%) blur(20px)` |
| **Vercel** | Geist Sans / Geist Mono | 48px / 600 | **-2.4 to -2.88px** (most aggressive) | 18px / 400, LH 1.56 | Workflow triad: `#0a72ef` / `#de1d8d` / `#ff5b4f` + link `#0072f5` | Pure achromatic: `#171717` (not true black) / `#ffffff` | 8px, skips 20/24 (jumps 16→32) | 2/4/6/8/12/64/100/9999/50% | Subtle hover shadow intensification |
| **Linear** | Inter Variable + Berkeley Mono | 72px / **510** (signature between-weight) | -1.584px | 16px / 400, LH 1.50 | Brand Indigo `#5e6ad2` / accent `#7170ff` | Dark-native luminance stack: `#08090a` / `#0f1011` / `#191a1b` / `#28282c` | 8px, micro 7/11/19/22 | 2/4/6/8/12/22/9999/50% | Depth via bg-opacity stepping, no shadows |
| **Raycast** | Inter + GeistMono + SF Pro Text | 64px / 600 | 0 display, **+0.2 to +0.4px on body** (contrarian) | 16px / **500** (not 400 — dark mode legibility) | Raycast Red `#FF6363` (punctuation only) + Blue `hsl(202,100%,67%)` | Blue-tinted near-black: `#07080a` / `#101111` / `#1b1c1e` | 8px | 2/4/6/8/9-11/12/16/20/86+(pill)/50% | **Opacity 0.6 hover** (not color change) + multi-layer inset shadows |

---

## Common patterns (all 4 agree)

1. **8px base spacing** universally, with micro-step additions (Apple 2-11px, Linear 7/11/19/22, Vercel 1/3/5). **None use a pure 4px base.**
2. **Inter-family or custom geometric sans** with OpenType features enabled globally (Vercel `liga`, Linear `cv01/ss03`, Raycast `calt/kern/liga/ss03`, Apple via SF optical sizing).
3. **Monospace companion** for technical identity in every system (Geist Mono, Berkeley Mono, SF Pro Icons).
4. **Weight ceiling at 600** (Linear caps at 590, others at 600). **Never 700 for display.**
5. **Near-black instead of pure black** for primary text/backgrounds: Apple `#1d1d1f`, Vercel `#171717`, Linear `#08090a`, Raycast `#07080a`. **Nobody uses `#000` except Apple in hero sections.**
6. **Single chromatic accent budget** — every system reserves its accent color strictly for interactive/brand moments. No decorative color usage.
7. **Shadow-as-border technique** in dark systems (Linear + Vercel + Raycast) using `0 0 0 1px` rings instead of CSS borders — works better with translucent surfaces.
8. **Pill CTAs at extreme radii** (Apple 980px, Raycast 86px+, Vercel 9999px for badges).

---

## Meaningful divergences

- **Letter-spacing direction** is the biggest split. Apple/Vercel/Linear go aggressively **negative** even on body text. **Raycast goes positive (+0.2 to +0.4px)** specifically because dark-mode legibility benefits from airy tracking. Deliberate contrarian choice.
- **Base body weight.** Raycast uses **500** (not 400) for body to prevent dark-mode text thinness. Others use 400. **Rule: if your project is dark-default, consider weight 500 for body.**
- **Linear's signature weight 510** sits between Inter's 400 and 500 — a between-weight impossible without Inter Variable. Makes Linear look distinctly like Linear.
- **Vercel skips 20/24px** from spacing scale (jumps 16→32). Unique to their brand.
- **Elevation strategy diverges by mode:**
  - Apple: flat, occasional soft diffused shadow, background contrast does the work
  - Vercel: multi-layer shadow stacks (border + ambient + inner `#fafafa` highlight)
  - Linear: luminance stepping via bg opacity, no shadow elevation
  - Raycast: macOS-native multi-layer with inset top highlights + inset bottom darks (simulates physical glass)
- **Motion language is mostly unspecified** in all 4 DESIGN.md files. **No named durations, no easing curves, no spring mentions.** Raycast's one explicit rule: "opacity 0.6 on hover, not color change." **This is the biggest shared gap — the standard fills it.**
- **Color space:** None use OKLCH. Apple/Vercel hex, Linear/Raycast mix hex + HSL. **The standard is ahead of the curve here.** OKLCH enables perceptually uniform light↔dark conversion which these systems can't do cleanly.
- **Accent strategy:**
  - Apple: 1 blue, strictly interactive
  - Linear: 1 indigo, strictly CTA/interactive
  - Vercel: 3 workflow colors (dev/preview/ship) marking pipeline stages
  - Raycast: red as punctuation (hero stripe) + blue as interactive — dual role

---

## Surprising tokens worth stealing

- **Apple 2.41 line-height on standard buttons** — extreme vertical breathing for button text. Consider for hero CTAs.
- **Vercel's 0.44rem (7px) micro-badge at weight 700 uppercase** — breaks their own "no weight 700" rule. Allowed because it's micro-context.
- **Vercel's card shadow requires inner `#fafafa 0 0 0 1px` ring** to "glow from within" — non-negotiable in their system. Worth trying in projects with white bg.
- **Linear's `"cv01", "ss03"` OpenType features** are declared fundamental: "without them, it's generic Inter, not Linear's Inter." **Rule: when you pick Inter, always turn on alternate glyphs.**
- **Raycast keyboard key caps use a 5-layer shadow stack** simulating physical press depth. Dedicated elevation level just for keyboard shortcut displays. Cool idea for any project showing ⌘K hints.
- **Apple's pill buttons use 980px radius** specifically — echoes their 980px max content width. Meta number.
- **Linear's button backgrounds live at `rgba(255,255,255,0.02)`** — functionally invisible fill. Structure comes entirely from border.
- **Raycast explicitly bans single-layer shadows** — all shadows must come in outer + inset pairs.

---

## One-line aesthetic per brand

- **Apple** — Cinematic reverence. A gallery where products are sculptures and the UI retreats to invisibility. Binary black/light-gray section pacing. Single blue accent. 980px pills.
- **Vercel** — Code-minified typography. Compressed Geist (-2.4px tracking) on pure white. Shadow-as-border instead of CSS borders. Achromatic except workflow-stage accents.
- **Linear** — Dark-mode-native precision. Darkness as medium (not overlay). Inter 510 signature weight. Luminance stepping for elevation. Ultra-thin semi-transparent white borders.
- **Raycast** — macOS-native obsidian instrument. Blue-tinted near-black. Multi-layer inset shadows simulating physical glass. Red as punctuation. Positive tracking for airy dark-mode readability.

---

## What the standard locks based on this validation

| Token/rule | Confirmed by | Status in standard |
|---|---|---|
| Near-black over pure black for dark bg | 4/4 brands | ✅ `oklch(0.14 0.01 240)` |
| Weight ceiling at 600 | 4/4 brands | ✅ `--weight-semibold: 600`, no 700 for display |
| 8px base spacing | 4/4 brands | ⚠️ Standard uses 4px base for tighter micro-steps — CONSCIOUS departure, 4/8/12/16 scale is compatible mental model |
| Single accent budget | 4/4 brands | ✅ "Single accent, scarce usage" rule |
| Shadow-as-border on dark | 3/4 brands (Apple doesn't) | 📝 Add note to materials section — optional technique for dark-mode projects |
| Mono companion always | 4/4 brands | ✅ `--font-mono` stack mandatory |
| Motion language unspecified | 0/4 specify | ✅ Standard fills the gap with spring curves + duration ladder |
| OKLCH color space | 0/4 use | ✅ Standard is ahead — OKLCH-only mandate |
| Negative display tracking | 3/4 brands (Raycast positive) | 📝 Display default `-0.025em`, but note Raycast-style positive tracking for dark-heavy projects |
| Base body weight | 3/4 use 400, Raycast 500 | 📝 Default 400 but recommend 500 for dark-default projects |

---

## Gaps in the brand DESIGN.md files

None of the 4 fetched DESIGN.md files included:
- Named motion tokens / durations / easing curves
- OKLCH or perceptually-uniform color space usage
- Explicit accessibility floors (contrast ratios, tap target minimums, `prefers-*` handling)
- Density mode toggles

**This means Chris's personal standard is genuinely ahead of the reference set in 4 categories.** Worth noting in the DESIGN.md decisions log.

---

## Sources

- https://getdesign.md/design-md/apple/DESIGN.md
- https://getdesign.md/design-md/vercel/DESIGN.md
- https://getdesign.md/design-md/linear.app/DESIGN.md
- https://getdesign.md/design-md/raycast/DESIGN.md

Fetched 2026-04-12 via parallel curl.
