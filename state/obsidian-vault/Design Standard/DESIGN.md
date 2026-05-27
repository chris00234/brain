# Chris Cho — Personal Design Standard

> **This is the cross-project frontend foundation.** Every frontend Chris builds — brain-ui, brain-control, and any future project — inherits from this document. Projects add their own signature on top (display font, accent color, density, vibe) but the chassis is locked here: type system, spacing, color architecture, motion language, materials, accessibility floor.
>
> **Indexed in brain RAG.** Any agent (Liz, Ellie, Jenna, Sage, Claude Code in any directory) can retrieve this via `brain_recall("design standard")` or `brain_recall("how should I design the frontend")`.

---

## 1. Philosophy

This standard is **the foundation, not the finish**. It locks the *structural* decisions an Apple-pedigree UI shares everywhere — type system mechanics, spacing math, motion physics, material philosophy, accessibility floor, color architecture — while leaving the *expressive* decisions to each project.

Think of it as the chassis of a car: every project rides on the same suspension and pedal feel; the body is what makes one project look like a research instrument and another look like a cyberpunk rig.

**Apple-pedigree means:** content-first, typography doing heavy lifting, restrained color, scarce accent usage, generous whitespace, spring-physics motion, dark mode treated as first-class, accessibility as floor not afterthought.

---

## 2. What's locked / what's free

This is the contract. Lock the right things, leave the rest free.

| Category | LOCKED in standard | FREE per project |
|---|---|---|
| **Typography** | Font *roles* (display/text/mono), modular scale 1.25, weight-pairing rule, line-heights (1.5 body / 1.2 display), `tnum`/`ss01` feature discipline | Specific display font, which density mode ships |
| **Spacing** | 4pt base unit, scale `4/8/12/16/20/24/32/40/48/64/96`, 16pt edge-margin floor, 44pt tap-target floor | Default content padding, sidebar widths, breakpoint values |
| **Color** | OKLCH only, neutral scale shape, role tokens, dark-via-lightness-shift, "single accent, scarce usage" discipline, semantic colors locked | Accent hue, true-black vs near-black dark mode |
| **Shape** | Continuous corner philosophy, device-concentric rule, scale `4/6/8/12/16/24` | Default card/button radius from scale |
| **Materials** | Blur scale `10/20/30/50/80`, vibrancy rule, elevation-via-lightness | Whether project uses heavy "Liquid Glass" blur |
| **Motion** | Spring-over-easing, four named curves, duration ladder `120/180/240/400/600ms`, `prefers-reduced-motion` floor, no entrance choreography default | Whether project gets a signature motion moment |
| **Density** | Three modes defined (compact 13 / standard 14 / comfortable 16), tap-target floor | Which mode the project ships |
| **Iconography** | Weight-pairs-with-text rule, no decorative-circle anti-pattern, Lucide React as cross-platform default | Custom icon set if project needs one |
| **Accessibility** | WCAG 2.2 AA contrast, focus-visible always, `prefers-*` honored | — (never free) |
| **Vibe** | — | Entirely free. One-sentence aesthetic thesis per project. |

**Rule of thumb:** if I changed it, would *two different projects of mine* both still feel like my work? Yes → lock it. No → it's expression, leave free.

---

## 3. Typography system

### Font roles (LOCKED)

Three roles, each with one job. Projects pick which specific fonts fill each role; the *role structure* never changes.

| Role | Purpose | Default |
|---|---|---|
| **Text** | Body, UI labels, buttons, everything not-display, not-data | `-apple-system` first (real SF Pro on Apple devices) → Geist Sans → Inter |
| **Display** | Page titles, hero moments, punctuation serif/display fonts | Defaults to text font — projects override with their signature |
| **Mono** | Tables, timestamps, code, metrics, anything needing tabular alignment | `ui-monospace` → SF Mono → Geist Mono → JetBrains Mono |

### Font stack (defined in `tokens.css`)

```css
--font-text:
  -apple-system, BlinkMacSystemFont, 'Geist Sans', 'Inter',
  system-ui, 'Segoe UI', Roboto, 'Helvetica Neue', sans-serif;

--font-display:
  -apple-system, BlinkMacSystemFont, 'Geist Sans', 'Inter',
  system-ui, sans-serif;

--font-mono:
  ui-monospace, 'SF Mono', 'Geist Mono', 'JetBrains Mono',
  Menlo, Consolas, monospace;
```

**Why `-apple-system` first:** on Chris's Mac, his iPhone, and most consumers of anything he ships, this renders as *actual SF Pro* for zero bytes, zero HTTP requests. Same strategy Apple.com uses. Geist Sans is the closest free SF-analog for Linux/Windows fallback; Inter is the second fallback.

### Modular scale (LOCKED — ratio 1.25)

| Token | Size (at 14px base) | Line height | Typical use |
|---|---|---|---|
| `--text-xs` | 12px | 16px | Captions, helper text |
| `--text-sm` | 13px | 18px | Pills, table meta, small labels |
| `--text-base` | **14px** | 20px | Body, buttons, inputs, table cells |
| `--text-md` | 16px | 24px | Section subheads |
| `--text-lg` | 18px | 28px | Large labels |
| `--text-xl` | 22px | 28px | Page subtitles |
| `--text-2xl` | 28px | 34px | Section titles |
| `--text-3xl` | 36px | 42px | Display headers (serif if used) |
| `--text-4xl` | 48px | 52px | Hero sub |
| `--text-5xl` | 64px | 66px | Hero |
| `--text-hero` | 112px | 106px | Marketing landing only |

Base size shifts with density mode (see §7). At standard density, base is 14px. At compact, 13px. At comfortable, 16px.

### Weight pairing rule (LOCKED)

Display fonts at display sizes use `400` or `500` *only*. Never `bold 700` on serif display — it destroys the typographic voice. Body gets `400` regular + `500` medium for emphasis + `600` semibold for strong emphasis. Never exceed 3 weights on one screen.

### Feature settings (LOCKED — always on)

```css
body {
  font-feature-settings: 'ss01' on, 'cv11' on;  /* Inter/Geist alternates */
}

.mono, code, .tabular {
  font-feature-settings: 'tnum' on, 'zero' on;  /* tabular numerals */
}
```

---

## 4. Spacing & density

### Scale (LOCKED)

Base unit **4pt** (not 8pt — half-step for tighter UI). Scale:

```
4 · 8 · 12 · 16 · 20 · 24 · 32 · 40 · 48 · 64 · 96
```

Tokens: `--space-1` through `--space-24`. See `tokens.css`.

### Density modes (LOCKED structure, FREE choice)

Three modes defined. Projects pick one as default. Users can override at runtime via `[data-density]`.

| Mode | Base font | Row height | Card padding | Vibe | Example projects |
|---|---|---|---|---|---|
| **compact** | 13px | 30px | 12px | power-user, dense | Linear, Raycast |
| **standard** (default) | 14px | 36px | 16px | daily tool, balanced | brain-ui, most dashboards |
| **comfortable** | 16px | 44px | 20px | reading-first, Apple native | marketing sites, blogs, Settings |

### Accessibility floors (LOCKED — never override below)

- **Tap target minimum:** 44px × 44px (`--tap-target-min`)
- **Edge margin minimum:** 16px (`--edge-margin-min`)
- **Line-length maximum (prose):** ~70 characters (45-75 range)

---

## 5. Color architecture

### Approach (LOCKED)

- **OKLCH only.** Not HSL, not hex. OKLCH gives perceptually uniform lightness shifts — critical for dark mode that converts cleanly from light.
- **Neutral scale + role tokens.** Components reference `--fg / --muted-fg / --bg / --surface / --border / --accent`, never raw neutral values. Changing the scale swaps the whole theme without touching components.
- **Dark-via-lightness-shift.** Dark mode is not a recolor — it's a lightness inversion of the same hue. iOS philosophy: elevation = shifting UP in lightness, not adding shadow.
- **Single accent, scarce usage.** One accent color per project. Used for interactive states (links, focus rings, active nav), not for decoration or visual variety. If you're reaching for color for variety, use typography weight or spacing instead.
- **Semantic colors are semantic.** Success/warning/error are locked to universal hues (green/amber/red). Never use them for non-status purposes.

### Role tokens (defined in `tokens.css`)

```
--bg              page background
--surface         card/panel background
--surface-elev    elevated surface (popover, dropdown)
--surface-sunken  inset surface (input, table header)
--fg              primary text
--muted-fg        secondary text
--faint-fg        tertiary text
--border          feather-thin separators
--border-strong   input borders, strong dividers
--accent          interactive (link, focus, active)
--accent-soft     hover fill, focus ring
--accent-fg       text on accent background
--success         semantic (green)
--warning         semantic (amber)
--error           semantic (red)
```

### Default accent (FREE — projects override)

Standard ships with **Apple systemBlue** as the default accent so a fresh project has a usable theme on first paint:

```css
--accent:      oklch(0.58 0.21 250);  /* ~#007AFF light */
--accent-soft: oklch(0.96 0.04 250);
```

Every project is *expected* to override. brain-ui uses electric cyan `oklch(0.58 0.19 230)`. brain-control will use something else. New projects pick their own.

### Dark mode (LOCKED approach, FREE depth)

- **Default dark mode:** `--bg: oklch(0.14 0.01 240)` — near-black with faint blue, not pure black. More forgiving for text rendering.
- **OLED opt-in:** `data-theme-oled="true"` drops to pure `oklch(0 0 0)`. Use only on projects designed for OLED devices.
- **Accent glows brighter in dark mode** — shifted up in lightness (+0.10) for contrast and life.

### What to never do

- Purple/violet gradient as a primary accent
- Rainbow status coding (six colors for six states)
- Gradient buttons as primary CTAs
- Decorative color (color as garnish, not signal)

---

## 6. Shape language

### Continuous corner philosophy (LOCKED)

Apple's shapes are **continuous (squircle)** — a G2-continuous curve with no visible inflection point. CSS `border-radius` approximates this, but not perfectly. The upcoming `corner-shape: squircle;` CSS property will give us the real thing; until then, use `border-radius` and accept the approximation.

When it ships, projects should add:
```css
.card, .button, .input { corner-shape: squircle; }
```

### Radius scale (LOCKED)

| Token | Value | Use |
|---|---|---|
| `--radius-xs` | 4px | Pills, small chips |
| `--radius-sm` | 6px | Tags, small buttons |
| `--radius-md` | 8px | Cards, buttons, inputs (default) |
| `--radius-lg` | 12px | Panels, dialogs |
| `--radius-xl` | 16px | Modals |
| `--radius-2xl` | 24px | Sheets, full dialogs |
| `--radius-full` | 9999px | Avatars, status dots |

### Device-concentric rule

Inner element corners should equal `outer_radius - inset` so all corners share a common center point. When nesting a card inside a container with 24px radius and 16px inset padding, the inner card gets 8px radius (24 - 16 = 8). This creates visual harmony; skipping it looks sloppy at any distance.

---

## 7. Materials & elevation

### Approach (LOCKED)

- **Elevation via lightness shift**, not drop shadow. Shadows are auxiliary, not primary. A card feels elevated because its background is lighter than its container, not because it casts a shadow.
- **Vibrancy/blur for floating UI** — tooltips, popovers, nav bars over content. Use `backdrop-filter: blur(var(--blur-*))`.
- **Dark mode materials are dark-tinted translucency**, not light materials inverted.

### Blur scale (LOCKED)

| Token | Value | Use |
|---|---|---|
| `--blur-ultra-thin` | 10px | Floating badges, subtle overlays |
| `--blur-thin` | 20px | Tooltips, nav bars over content |
| `--blur-regular` | 30px | Popovers, dropdowns, menus |
| `--blur-thick` | 50px | Alerts, sheets, action sheets |
| `--blur-chrome` | 80px | Sidebars (macOS), rigid nav |

### Elevation shadows (auxiliary, LOCKED values)

| Token | Purpose |
|---|---|
| `--elev-0` | Flat — base surface |
| `--elev-1` | Subtle lift (cards, rows) |
| `--elev-2` | Standard floating (buttons, inputs on focus) |
| `--elev-3` | Dialogs, dropdowns |
| `--elev-4` | Modals, drawers, full-screen sheets |

Dark mode shadows are heavier — see `tokens.css`.

### Liquid Glass (FREE, opt-in)

Projects may opt into iOS 26-style heavier translucency (40-80px blur with reactive tinting) for a more "glass-forward" feel. Default is the standard material scale; glass is an override.

---

## 8. Motion language

### Philosophy (LOCKED)

**Spring physics over time easings.** Springs are interruptible, physically grounded, and match the iOS SwiftUI approach. CSS doesn't have real springs, so we approximate via `cubic-bezier`.

**No entrance choreography on page load.** Data renders instantly. Hover, focus, click, drag all animate — but pages don't stagger in.

**`prefers-reduced-motion` is non-negotiable.** Reduced-motion users get instant transitions. Always. (Handled automatically in `tokens.css`.)

### Named curves (LOCKED)

| Curve | `cubic-bezier` | When to use |
|---|---|---|
| `--ease-smooth` | `(0.16, 1, 0.3, 1)` | Default — ease-out-expo, 95% of transitions |
| `--ease-snappy` | `(0.4, 0.8, 0.2, 1)` | Tight response — drags, interactive gestures |
| `--ease-bouncy` | `(0.34, 1.56, 0.64, 1)` | Slight overshoot — toggles, spring-on-click |
| `--ease-instant` | `(0, 0, 0, 1)` | Straight-through — no curve |

### Duration ladder (LOCKED)

| Token | Value | Use |
|---|---|---|
| `--dur-instant` | 0ms | No animation |
| `--dur-quick` | 120ms | Micro: focus ring, hover background, button press |
| `--dur-short` | 180ms | Popover, dropdown, tooltip |
| `--dur-medium` | 240ms | Dialog, drawer, theme toggle |
| `--dur-long` | 400ms | Page transition, card reorder |
| `--dur-slow` | 600ms | Emphasis, rare |

### Rules

- **Default to `--ease-smooth` + `--dur-quick`** unless you have a reason.
- **Never use `transition: all`.** Always list specific properties.
- **Match duration to distance** — a 2px hover shift at 180ms looks sluggish; use 120ms.
- **Signature moments get more budget** — brain-ui's ⌘K summon is a 3D flip at 400ms. It's allowed because it's the *one* signature moment per project, not the default.

---

## 9. Iconography

### Default: Lucide React (LOCKED — for React projects)

Lucide is the cross-platform default. Consistent stroke width (1.5px), comprehensive library, free, actively maintained, TypeScript-native, tree-shakeable.

### On Apple platforms

SF Symbols via the system font stack gives real SF Symbols for free on iOS/macOS apps. The web cannot legally use them in third-party sites, but you can approximate via Lucide.

### Rules (LOCKED)

- **Icon weight pairs with text weight.** Regular text = regular icon. Bold text = bold icon (at 2px stroke).
- **Outline = inactive, filled = active.** Tab bars, toggle states. Never mix the two conventions on one screen.
- **No decorative circles around icons.** The "feature icon in colored circle on feature card" pattern is AI-slop. Never use it.
- **Icon size pairs with text line-height** — 14px text (line-height 20px) pairs with 14px icon (or 16px for emphasis). Never icons larger than their adjacent text line-height without intentional hierarchy.

### Anti-patterns

- Icon-in-colored-circle feature grids
- Emoji as UI icons (except in specific playful contexts)
- Mixed icon sets on one screen
- Icons without labels in primary navigation (tab bars excepted)

---

## 10. Accessibility floor (NEVER FREE)

All of these are locked. No project can opt out.

- **Contrast: WCAG 2.2 AA minimum.** 4.5:1 for normal text, 3:1 for large text (18px+ or 14px+ bold). Preview page includes contrast readouts.
- **Focus visible: always.** `*:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }`. Never `outline: none` without replacement.
- **Tap targets: 44px minimum.** See §4.
- **`prefers-reduced-motion`: honored.** See §8.
- **`prefers-color-scheme`: honored.** Dark mode respects system unless explicitly overridden by user.
- **`prefers-contrast`: honored where possible.** `@media (prefers-contrast: more)` bumps contrast to AAA.
- **Semantic HTML.** `<button>` for buttons, `<nav>` for nav, `<main>` for main. `<div onClick>` is a smell.
- **Keyboard navigation works.** Every interactive element reachable via Tab, every trigger activatable via Enter/Space.
- **Screen reader labels.** `aria-label` on icon-only buttons. Meaningful alt text on images.

---

## 11. Project expression checklist

When starting a new project that inherits from this standard, answer these **5 questions** in a short project-level `DESIGN.md`. That's the full expression layer.

### The 5 questions

1. **Vibe word** — One sentence. What is the aesthetic thesis? ("Owner-operator research instrument." "Warm reading room." "Cyberpunk topology rig." "Precise financial tool.") This drives every other decision.

2. **Display font** — Which font fills the `--font-display` slot for page titles and hero moments? Options to consider: Instrument Serif (literate, research), General Sans (clean, geometric), Clash Display (modern, confident), Geist Mono (technical, brutalist), Fraunces (editorial, playful). Or default to same as text.

3. **Accent hue** — What's the one accent color? Override `--accent` with your project's OKLCH. Remember: scarce usage, never decoration.

4. **Density mode** — `compact` (power tool, 13px), `standard` (balanced, 14px), or `comfortable` (reading, 16px)? Set `data-density` on your root.

5. **Signature moment** — What's the one unexpected polish moment that makes this project yours? (brain-ui: Brain3D + ⌘K flip. Apple: parallax product scroll. Linear: cmd palette haptic.) Most projects get zero signature moments by default; pick one only if it earns its place.

### Minimal project `DESIGN.md` template

```markdown
# [Project] Design

> **Inherits from:** `~/design-standard/DESIGN.md`

## Expression

- **Vibe:** [one sentence]
- **Display font:** [name] + CDN/source
- **Accent:** `oklch(...)` light / `oklch(...)` dark — [one-line rationale]
- **Density:** [compact/standard/comfortable]
- **Signature moment:** [what + why — or "none"]

## Overrides

Any deviations from the standard beyond the 5 above. Each deviation needs a reason.
```

That's it. 20 lines of project DESIGN.md, fully inheriting 500 lines of standard.

---

## 12. How to consume this standard

### From a new project

1. **Read via brain RAG:** `curl -H "Authorization: Bearer $SECRET" "http://127.0.0.1:8791/recall?q=design+standard&n=3" | jq`, or use MCP `brain_recall("design standard")` from Claude Code.
2. **Copy `tokens.css`** into your project's CSS root.
3. **Import it first**, before any component styles: `@import url('./tokens.css');`
4. **Write your project's `DESIGN.md`** using the template in §11.
5. **Override the FREE tokens** in your project's CSS: `--accent`, `--font-display`, `[data-density]`.
6. **Build.** Everything else is already decided.

### From an agent (Liz, Sage, Jenna, Market, Claude Code)

Any time an agent is asked to design or implement a frontend for any project, the first action should be:

```
brain_recall("design standard")
```

This returns the full standard. The agent then reads §11 to understand what the project needs to specify, and proceeds with implementation.

### From Chris (daily use)

- `~/design-standard/DESIGN.md` — the source. Edit here, changes propagate everywhere via symlink + nightly canonical pipeline.
- `~/design-standard/specimens/preview.html` — visual proof. Open in browser, toggle density/theme, eyeball before committing to a new decision.
- `~/design-standard/tokens.css` — the portable artifact. `cat | pbcopy` into any new project.

---

## 13. Decisions log

| Date | Decision | Rationale |
|---|---|---|
| 2026-04-12 | Standard created via `/design-consultation` pipeline + manual authorship | Chris wanted to pull back from brain-ui's Instrument Panel direction and establish a cross-project foundation before building more projects. Apple-style preference stated explicitly. |
| 2026-04-12 | Font stack: `-apple-system` first, Geist Sans fallback | Apple devices get real SF Pro for zero bytes. Geist is closest free SF-analog (over Inter) for Linux/Windows. Same strategy as apple.com. |
| 2026-04-12 | Default accent: Apple systemBlue | Defensible default for fresh projects; every project overrides. Existence of a default matters for 0-to-1 usability. |
| 2026-04-12 | Default density: standard 14px | 13px too cramped for default, 16px too SaaS-generic. 14 is the power-tool sweet spot brain-ui already uses. |
| 2026-04-12 | brain-ui reconciliation: light-touch inherit note, not full refactor | brain-ui was pixel-calibrated 30 min before the standard existed. Full refactor risks chained edits when standard still evolving. Re-evaluate in 3 months. |
| 2026-04-12 | Semantic colors locked, accent free | Success should look like success in every project; accent should express identity. |
| 2026-04-12 | Motion: spring physics approximated via cubic-bezier | CSS lacks real springs; `cubic-bezier(0.16, 1, 0.3, 1)` is the closest ease-out-expo approximation of SwiftUI `.smooth`. |
| 2026-04-12 | Indexed into brain RAG via canonical symlink + memory seed | Chris explicitly required cross-project retrievability. Standard lives in `~/design-standard/` as source; symlinked to `~/server/knowledge/canonical/design/personal_standard.md` for nightly ChromaDB indexing; immediate `/memory` POST for instant searchability. |

---

## Appendix A — Token reference (abbreviated)

Full definitions in `~/design-standard/tokens.css`. Light mode values below; dark mode swaps in `[data-theme="dark"]`.

| Token | Value | Role |
|---|---|---|
| `--bg` | `oklch(0.985 0.003 75)` | Page background |
| `--fg` | `oklch(0.15 0.01 240)` | Primary text |
| `--muted-fg` | `oklch(0.55 0.005 240)` | Secondary text |
| `--border` | `oklch(0.94 0.003 240)` | Feather-thin separators |
| `--accent` | `oklch(0.58 0.21 250)` | Interactive (default Apple blue) |
| `--success` | `oklch(0.65 0.17 155)` | Semantic green |
| `--warning` | `oklch(0.75 0.17 75)` | Semantic amber |
| `--error` | `oklch(0.60 0.24 25)` | Semantic red |
| `--radius-md` | `8px` | Default card/button radius |
| `--dur-quick` | `120ms` | Default transition duration |
| `--ease-smooth` | `cubic-bezier(0.16, 1, 0.3, 1)` | Default transition easing |
| `--space-4` | `1rem` (16px) | Default content padding |
| `--tap-target-min` | `2.75rem` (44px) | Accessibility floor |

---

## Appendix B — References

- **Apple HIG** — source for SF Pro, color system, materials, motion principles. Condensed at `~/design-standard/references/apple-hig-notes.md`.
- **getdesign.md 4-brand spot-check** — Apple, Vercel, Linear, Raycast token validation. At `~/design-standard/references/getdesign-spotcheck.md`.
- **brain-ui expression** — first reference implementation. `~/server/brain-ui/DESIGN.md`.
