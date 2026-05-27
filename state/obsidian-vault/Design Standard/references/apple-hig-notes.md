# Apple HIG notes — condensed for personal design standard

**Purpose:** Distilled reference material from Apple Human Interface Guidelines and Apple.com for the personal cross-project design standard. Captures the Apple design language primitives that inform `~/design-standard/DESIGN.md`.

**Source research:** extracted by a Claude research subagent on 2026-04-12 from developer.apple.com/design/human-interface-guidelines, Wikipedia (iOS 26 Liquid Glass), sarunw.com, hackingwithswift.com, and publicly documented UIKit/SwiftUI constants. Third-party sources used because HIG pages are JS-rendered and unfetchable via scrapers.

---

## SF Pro family

| Variant | Weights | Optical size | Use |
|---|---|---|---|
| **SF Pro Display** | 9 (Ultralight → Black) + italics | **20pt and above** | Headlines, titles, hero. Tighter letter-spacing, smaller apertures. 4 widths (normal, condensed, compressed, expanded). |
| **SF Pro Text** | 9 + italics | **Below 20pt** | Body, UI labels, captions. Larger apertures, more generous tracking. |
| **SF Pro Rounded** | 9 | All | Softer variant for playful/health/fitness contexts (Apple Watch Activity, Fitness+). |
| **SF Mono** | 6 + italics | All | Xcode, Terminal, code blocks. |
| **SF Compact / Compact Rounded** | 9 | All | watchOS — narrow columns, small screens. |
| **New York** (companion serif) | 6 | Variable | Used sparingly in Books, Podcasts headers. |

**Licensing:** SF Pro is licensed **solely for mockups of apps running on iOS/macOS/tvOS/watchOS**. Not permitted for general web use. Apple serves SF Pro woff2 files from cdn.apple.com exclusively for apple.com and their own web properties.

**Web implication:** Use the `-apple-system` font stack, which on Apple devices resolves to the real SF Pro via the operating system. On non-Apple devices it falls through to the next font in the stack (our standard uses Geist Sans as the primary non-Apple fallback).

---

## Apple color system

### iOS system colors (sRGB hex, light / dark)

| Token | Light | Dark |
|---|---|---|
| systemRed | `#FF3B30` | `#FF453A` |
| systemOrange | `#FF9500` | `#FF9F0A` |
| systemYellow | `#FFCC00` | `#FFD60A` |
| systemGreen | `#34C759` | `#30D158` |
| systemMint | `#00C7BE` | `#63E6E2` |
| systemTeal | `#30B0C7` | `#40C8E0` |
| systemCyan | `#32ADE6` | `#64D2FF` |
| **systemBlue** | **`#007AFF`** | **`#0A84FF`** |
| systemIndigo | `#5856D6` | `#5E5CE6` |
| systemPurple | `#AF52DE` | `#BF5AF2` |
| systemPink | `#FF2D55` | `#FF375F` |
| systemBrown | `#A2845E` | `#AC8E68` |

### Neutral scale

| Token | Light | Dark |
|---|---|---|
| systemGray | `#8E8E93` | `#8E8E93` |
| systemGray2 | `#AEAEB2` | `#636366` |
| systemGray3 | `#C7C7CC` | `#48484A` |
| systemGray4 | `#D1D1D6` | `#3A3A3C` |
| systemGray5 | `#E5E5EA` | `#2C2C2E` |
| systemGray6 | `#F2F2F7` | `#1C1C1E` |

### Semantic tokens (dynamic, auto-adapt)

| Token | Light | Dark |
|---|---|---|
| label | `#000` 100% | `#FFF` 100% |
| secondaryLabel | `rgba(60,60,67,0.60)` | `rgba(235,235,245,0.60)` |
| tertiaryLabel | `rgba(60,60,67,0.30)` | `rgba(235,235,245,0.30)` |
| quaternaryLabel | `rgba(60,60,67,0.18)` | `rgba(235,235,245,0.16)` |
| systemBackground | `#FFFFFF` | `#000000` |
| secondarySystemBackground | `#F2F2F7` | `#1C1C1E` |
| tertiarySystemBackground | `#FFFFFF` | `#2C2C2E` |
| separator | `rgba(60,60,67,0.29)` | `rgba(84,84,88,0.65)` |
| link | `#007AFF` | `#0A84FF` |

### Dark mode philosophy

- **True black `#000000` on OLED devices** for `systemBackground` — rationale: OLED energy efficiency + infinite contrast.
- **Elevation conveyed by stepping UP in lightness** (opposite of Material Design's elevation-via-shadow). Base `#000` → elevated `#1C1C1E` → further elevated `#2C2C2E` → `#3A3A3C`. No drop-shadow elevation on iOS.
- Two parallel palettes: `system*` (grayscale-on-black base) and `systemGrouped*` (black base, elevated cards). Grouped for table/list screens like Settings.

---

## Spacing & density

- **Base unit: 8pt.** Many compositions use 4pt sub-grid for icons/small elements.
- **Common scale:** 4, 8, 12, 16, 20, 24, 32, 40, 48, 64pt.
- **Screen edge margins:** 16pt (compact width), 20pt (regular width), 32-44pt (iPad).
- **Minimum tap target: 44×44pt.** Hard rule.
- **Stack spacing defaults:** 8pt tight, 12pt default, 16-20pt loose, 24+pt sectional.
- **Density philosophy:** generous whitespace, content breathes, avoid cramped clusters. Apple.com marketing uses ~92px top / 140px bottom section padding on desktop, scaling down.

---

## Corner radius — continuous (squircle)

- **Continuous corners** are the Apple default, NOT standard CSS `border-radius`. Uses a super-ellipse / G2-continuous curve so the corner has no visible inflection point.
- SwiftUI: `.cornerRadius(r, style: .continuous)`.
- **Device-concentric rule:** inner element corners should equal `outer_corner - inset` so all corners share a common center. Example: iPhone screen 47.33pt → safe-area card inset 16pt → card radius 31.33pt.
- **Typical values:** 6pt chips, 8-10pt buttons, 12pt small cards, 16pt medium cards, 20-24pt sheets, 28-33pt fullscreen sheets matching device.
- **CSS approximation:** `border-radius` + future `corner-shape: squircle;` CSS property. Figma "smoothing 60%" is the common proxy.

---

## Materials (translucency)

| Material | Effect | Typical use |
|---|---|---|
| **ultraThinMaterial** | Highest translucency, subtle tint | Floating badges, lock screen notifications |
| **thinMaterial** | Light translucency | Tab bars, secondary toolbars, iOS 15+ nav over images |
| **regularMaterial** | Balanced | Standard popovers, menus, form sheet headers |
| **thickMaterial** | Heavy blur | Alerts, control center cards, action sheets |
| **chromeMaterial** | Most opaque "bar" look | Sidebars (macOS), rigid nav bars |

**Approximate backdrop-blur pixel values** (Apple keeps internal values undocumented):
- ultraThin: ~10px
- thin: ~20px
- regular: ~30-40px
- thick: ~50px
- chrome: ~60-80px

**Vibrancy-aware:** labels placed on a material auto-adopt vibrant colors (`UIVibrancyEffect`) that desaturate and blend for legibility.

**Dark mode materials** invert: light blurs become dark-tinted gaussian with ~80% opacity dark gray.

---

## Motion

### Philosophy
Apple favors **interruptible, physically grounded springs** over time-based easings. CSS doesn't have real springs — we approximate via `cubic-bezier`.

### SwiftUI spring defaults
- `.spring()` — response 0.55, dampingFraction 0.825
- `.interactiveSpring()` — response 0.15, damping 0.86 (drag-follow)
- iOS 17+ named: `.smooth` (no bounce, response 0.5), `.snappy` (tight response 0.4, mild bounce), `.bouncy` (response 0.5, bounce 0.3)

### Durations
- **Quick / micro:** 100-200ms (tap feedback, toggle state)
- **Regular:** 250-350ms (standard transitions, sheet present, tab switch)
- **Slow / emphasis:** 400-600ms (modal present/dismiss, view push)
- **Extended:** 800ms+ (reserved for celebration moments, onboarding)

### Easing curves (time-based approximations)
- **Enter:** ease-out `cubic-bezier(0, 0, 0.58, 1)`
- **Exit:** ease-in `cubic-bezier(0.42, 0, 1, 1)`
- **Move:** ease-in-out `cubic-bezier(0.42, 0, 0.58, 1)`
- **iOS "default" curve:** ~`cubic-bezier(0.25, 0.1, 0.25, 1)`

### Principles
- **Responsive** — kicks off <100ms from input
- **Clarity** — motion explains hierarchy (where something came from / goes to)
- **Depth** — parallax + stacking order + scale-through
- **Interruptibility** — new gesture overrides in-flight animation
- **Reduced motion respect** — `prefers-reduced-motion` → crossfade instead

---

## Iconography — SF Symbols

- **SF Symbols 6+** ships ~6,000 symbols. Same license as SF Pro — Apple platforms only, not general web.
- **Weights (9):** Ultralight, Thin, Light, Regular, Medium, Semibold, Bold, Heavy, Black. Pair with matching SF Pro text weight.
- **Scales (3):** Small, Medium, Large — relative optical weight against adjacent text.
- **Rendering modes:** monochrome (default), hierarchical (single hue, layered opacity), palette (multiple colors per layer), multicolor (intrinsic colors like red heart).
- **Filled vs outline:** outline = default/inactive, filled = selected/active. Tab bar convention: `house` → `house.fill`.
- **Variable symbols (iOS 16+):** single symbol scrubbed 0-1 for progress/volume UI.

**Web alternative:** Lucide React is the closest free equivalent to SF Symbols' stroke style + comprehensiveness. The standard defaults to Lucide for web projects.

---

## Apple.com web aesthetic

*Separate from HIG which is for native apps.*

- **Font stack:** `"SF Pro Display", "SF Pro Icons", "Helvetica Neue", Helvetica, Arial, sans-serif` — Apple serves SF Pro woff2 directly from their CDN under a restricted license for apple.com only.
- **Color approach:** mostly monochrome — `#FFFFFF` background + off-black `#1D1D1F` text. systemBlue `#0066CC` for links. Product sections alternate with **true-black `#000000`** for dramatic hero moments (iPhone Pro, Mac Pro pages).
- **Layout:** full-bleed hero sections, 980px / 692px max content rails, 8-column product promo grid, sticky mini-nav, anchor-scroll navigation.
- **Motion:** scroll-driven reveals (intersection observer → fade-up + parallax), video-as-scroll (canvas frame sequences), sticky pin sections, cursor-reactive product shots.
- **Typography:** hero 48/40/32px by breakpoint. Section titles 40-56px. Body 17-19px.

---

## iOS 26 / macOS 26 Liquid Glass (2026 trend)

First major visual overhaul since iOS 7:

- **Liquid Glass material system** — unifies aesthetic across iOS/iPadOS/macOS/watchOS/visionOS. Elements take on "optical qualities of glass" — refraction, specular highlights, translucency **reactive to motion, content, and input** (gyroscope + content-aware).
- **Glass theme for icons** — new "Clear" home screen theme applies glass effect to icons, Dock, Control Center. Requires A16 Bionic or newer.
- **Translucency supersedes shadow** — even deeper move away from shadows as elevation cue. Layered transparency + refraction carries depth.
- **Dark mode evolution** — still true black `#000000` on OLED, but dark surfaces are now **translucent glass-on-black** rather than flat dark gray fills. Elevated layers read as frosted panels over black rather than `#1C1C1E` solid cards.
- **Blur values trend up** — Liquid Glass pushes backdrop blur noticeably heavier (40-80px) with subtle color tint inherited from underlying content.

---

## Web-safe font alternatives (for non-Apple devices)

Ranked by SF-Pro-likeness:

1. **`-apple-system, BlinkMacSystemFont, system-ui` stack** — on Apple devices, this IS SF Pro via the OS. Zero download, perfect fidelity. Falls back on non-Apple. **First choice.**
2. **Geist Sans** (Vercel, OFL, free) — 9 weights, Swiss-minimalist proportions closest to SF Pro among free fonts. Designed by Vercel as a dev/designer system font. Pairs with Geist Mono.
3. **Inter** (rsms, OFL, free) — industry-standard SF alternative. 9 weights + italics, variable optical sizing, 2000+ glyphs. Slightly wider and warmer than SF Pro.
4. **Helvetica Neue** — legally free on macOS/iOS but requires commercial webfont license. Not usable for the standard.

**The standard uses `-apple-system` → Geist Sans → Inter.** This gets real SF Pro to Apple users, Geist to everyone else, Inter as hard fallback.

---

## License matrix — what's free for web use

**Free (OFL / open source — safe for any website):**
- Inter, Geist Sans, Geist Mono, Geist Pixel, JetBrains Mono, IBM Plex Sans
- System font stack declarations (`-apple-system`, `system-ui`, `ui-monospace`)

**Licensed — NOT usable as webfont:**
- SF Pro family (Display, Text, Rounded, Compact)
- SF Mono
- New York
- Helvetica Neue (webfont license required)

---

## Research gaps

- Apple HIG developer.apple.com pages are JS-rendered; subagent could not fetch them directly and relied on well-documented third-party references + subagent training data.
- Exact backdrop-blur pixel values and spring damping constants are approximate — Apple keeps internal numeric values undocumented.
- iOS 26 Liquid Glass details primarily from Wikipedia and developer announcements, not production SDK documentation.

For pixel-exact fidelity on any specific Apple control, sample from a real iOS device or extract constants from UIKit headers.
