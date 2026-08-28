<!-- apps/frontend/.interface-design/system.md -->
# Lightbulb — Design System

Brand Identity Manual v1.0 (§05 Typography, §06 Colour, §07 Icons) is the source of truth.
Tokens live in `src/app/globals.css`; nothing else defines a colour. Operator direction:
calm when healthy, loud only on incidents. Borders only, no shadows; 4px spacing.

## The two grounds
The product is **cream** (`:root`). `/admin` is the manual's **reversed** treatment — cream
on midnight, not gray — via the `dark` class on `app/admin/layout.tsx`. Same utilities on
both; only the token values change.

| | cream (`:root`) | reversed (`.dark`) |
|---|---|---|
| `bg-background` / `text-foreground` | cream / midnight | midnight / cream |
| `bg-card`, `bg-popover` | cream | midnight 92% |
| `bg-secondary`, `bg-accent`, `bg-muted` | cream-deep | midnight 86% |
| `text-muted-foreground` / `border-border` / `border-input` | midnight 64% / edge / edge | cream 70% / cream 14% / cream 14% |
| `bg-primary` / `text-primary-foreground` | midnight / cream | cream / midnight |
| `ring` | midnight | bulb — on cream the bulb is 1.45:1; a ring must clear 3:1 |

## State pairs (`text-ok`, `border-warn`, …)
`--ok` `--warn` `--err` `--info` each carry a cream value and a `-reversed` value; the
`.dark` block re-points the token, so **never name a `-reversed` token in a component**.
ok = success/running · info = in-progress/links · warn = stopped/degraded · err = incident.
- A state colour marks text, an icon, a border or a thin rule — **never a fill**.
  Badge = `border border-ok text-ok`. Banner = `border-l-2 border-warn bg-card`.
- **Never colour alone**: every status keeps its word or icon.
- Unknown / unreadable renders neutral (`border-border text-muted-foreground`), never ok.

## Fonts (§05)
`font-sans` Source Sans 3 (UI, 400/500/600, never lighter) · `font-serif` Lora (prose,
headings) · `font-mono` Geist Mono (code and terminal output only).

## The no-literal rule
No hex, `rgb()`, `oklch()`, `hsl()`, `[#…]`, or Tailwind palette class (`zinc-*`,
`emerald-*`, `sky-*`, `amber-*`, `red-*`, `white`, `black`, …) outside the five files
the scanner skips: `globals.css` and `landing.css` (the token layer),
`components/landing/**` (marketing, its own palette), `lib/clerkAppearance.ts` (Clerk
takes hex, not `var()`), `*Brand.ts` (vendor marks), and tests (`__tests__/`,
`*.test.*`). Charts use `var(--chart-1..5)` (ok, info, warn, err, neutral — the fifth is
`--muted-foreground`, not a state colour, so no non-status series borrows one). Enforced by
`node scripts/brand-tokens-scan.mjs src` — must print 0. Admin primitives: `StatusChip` ·
`Tile` (`tone` border = severity) · `Card` · `MetricStat` · `ErrorBanner` · `LogRow` · `AuditRow`.
