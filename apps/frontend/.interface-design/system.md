<!-- apps/frontend/.interface-design/system.md -->
# Admin Operator Console — Design System

**Direction:** operator console for a live agent fleet. Calm when healthy, loud only on incidents.
**Surface:** existing DARK admin theme (zinc-900/40 cards, zinc-800 borders, zinc-100 text). NOT cream/forest — that is the main-app brand; the admin surface stays dark (no recolor).
**Depth:** borders-only (`border-zinc-800`); no shadows.
**Spacing base:** 4px (Tailwind scale).
**Radius:** `rounded-lg` cards, `rounded` chips.

## Semantic status colors (the operator register)
- running / healthy → emerald (`bg-emerald-500/15 text-emerald-300 border-emerald-500/30`)
- provisioning / in-progress → sky
- stopped / warn → amber
- error / incident → red
- unknown / unable-to-read → zinc  ← a degraded source read renders THIS, never green

## Components
- `StatusChip` — one status language everywhere (extracted from container/page.tsx).
- `Tile` — attention-strip cell; `tone` border encodes severity.
- `Card` — section container.
- `MetricStat` — tabular-nums metric.
