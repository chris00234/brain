# Chris Cho — Personal Design Standard

Cross-project frontend foundation. Apple-style chassis. Every project inherits from this.

## What's here

```
DESIGN.md                  # The standard. 13 sections. Source of truth.
README.md                  # This file.
tokens.css                 # Copy-pasteable CSS custom properties.
references/
  apple-hig-notes.md       # Distilled Apple HIG research.
  getdesign-spotcheck.md   # 4-brand validation (Apple, Vercel, Linear, Raycast).
specimens/
  preview.html             # Visual showcase. Open in browser.
```

## Three ways to use this

### 1. As an agent (Liz, Sage, Jenna, Market, Claude Code in any project)

```bash
SECRET=$(cat ~/.brain/credentials/.personal_webhook_secret)
curl -s -H "Authorization: Bearer $SECRET" \
  "http://127.0.0.1:8791/recall?q=design+standard&n=3" | jq
```

Or via MCP: `brain_recall("design standard")`.

The standard is indexed into brain's `canonical` ChromaDB collection (highest trust tier) via symlink at `~/server/knowledge/canonical/design/personal_standard.md`, plus an immediate memory seed in `semantic_memory`.

### 2. As a new-project starter

```bash
# Open the visual specimen to confirm the look
open ~/design-standard/specimens/preview.html

# Copy tokens into your new project
cp ~/design-standard/tokens.css /path/to/new-project/src/styles/

# Then in your project's root CSS:
#   @import url('./styles/tokens.css');
```

Then write a short project `DESIGN.md` answering the 5 expression questions (see `DESIGN.md` §11):
1. Vibe word
2. Display font
3. Accent hue
4. Density mode
5. Signature moment

### 3. As the author

```bash
# Edit the standard itself
$EDITOR ~/design-standard/DESIGN.md
$EDITOR ~/design-standard/tokens.css

# Changes propagate to the canonical mirror automatically (symlink).
# Brain re-indexes nightly at 2:00am via canonical_pipeline.
# Trigger immediately if needed:
curl -s -X POST -H "Authorization: Bearer $SECRET" \
  http://127.0.0.1:8791/jobs/canonical_pipeline
```

## The two-layer model

**Layer 1 (this standard):** chassis. Type system, spacing, color architecture, motion, materials, accessibility. Locked.

**Layer 2 (per-project):** body. Display font, accent hue, density mode, vibe, signature moment. Free.

See `DESIGN.md` §2 for the full locked/free contract.

## Reference implementations

- **brain-ui** — first expression. "Instrument Panel" aesthetic. Instrument Serif display + electric cyan accent + compact 14px density. At `~/server/brain-ui/DESIGN.md`.

## Current version

**v0.1** — 2026-04-12. Initial release.
