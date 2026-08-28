# The product on the brand manual — one token set for the whole app

**Disposable.** Delete in the PR that ships it. Owner direction 2026-08-28: *"ideally we
don't use shadcn we use the brand manual … the admin site uses the wrong colors … we
should make it consistent and correct … add the UI fixes to the main chat and the sign
up and sign in pages."*

Source of truth: Brand Identity Manual v1.0 (§05 Typography, §06 Colour, §06 State
values, §07 Icons, §10 Nomenclature). Where this spec and the manual differ, the
manual wins; where the manual is silent, this spec decides.

## What "not shadcn" means here

shadcn/ui **components stay** (`components/ui/*` — button, dialog, select, tabs, …).
What goes is shadcn's **palette**: the stock `oklch` grays in `.dark`, the Tailwind
`zinc/emerald/sky/amber/red` classes, and the old-brand hexes scattered through the
app. After this, every colour in the product resolves to a token named after the
manual, and the shadcn surface contract (`--background`, `--primary`, …) is just a
view onto those tokens so the components need no edits.

## Tokens (`globals.css` `:root`) — the only colours the product may use

| token | value | manual rule |
|---|---|---|
| `--midnight` | `#0F1A2B` | headers, dark UI, all body copy on cream |
| `--cream` | `#F5EFE2` | default light ground; never pure white |
| `--bulb` | `#F0C34E` | the light — one accent at a time; never type, never a page fill |
| `--brass` | `#B68235` | rules, small labels **on midnight**; never body copy on cream |
| `--cream-deep` | `#EFE8D9` | the one surface step on cream (hover/selected wash, inset inputs) |
| `--cream-deeper` | `#E7DECB` | the second step (pressed, table stripe) |
| `--edge` | `#DED5C2` | hairline borders on cream |
| `--edge-soft` | `#E8E0D0` | softer separation on cream |
| `--midnight-soft` | `color-mix(in srgb, var(--midnight) 80%, var(--cream))` | secondary text on cream |
| `--midnight-muted` | `color-mix(in srgb, var(--midnight) 64%, var(--cream))` | tertiary/metadata text — **measure ≥ 4.5:1 on cream** (60% computes to 4.44, which is why this is 64) |
| `--midnight-label` | `color-mix(in srgb, var(--midnight) 60%, var(--cream))` | §06's "small uppercase labels on cream at 60%" — eyebrows and table headers only, never body |
| `--midnight-faint` | `color-mix(in srgb, var(--midnight) 40%, var(--cream))` | disabled text/icons; non-text only otherwise |
| `--ok` / `--ok-reversed` | `#3A6B45` / `#7FB58D` | success — on cream / on midnight |
| `--warn` / `--warn-reversed` | `#96501A` / `#E09A55` | warning |
| `--err` / `--err-reversed` | `#96382B` / `#DB8C7C` | error |
| `--info` / `--info-reversed` | `#35566F` / `#93B4C9` | info |

Retired, **deleted not aliased**: `--forest`, `--forest-soft`, `--forest-tint`,
`--edge-forest`, `--gold`, `--ink`, `--ink-soft`, `--mute`, `--mute-soft`,
`--lamp-on/-dim/-out`, `--danger`, `--warn` (the single-value one), `--chart-*` as
oklch. Every reference is renamed at the call site (~211 files) — an alias would be a
second name for the same colour, which is exactly the drift this fixes. Aden's
"states as lamps" idea is retired with them: the manual defines the state pairs and
the owner asked for the manual.

**Landing is included in the rename** (it reads the same tokens) but its own design —
`landing.css`, the hero, the `data-lb-theme` mechanism — is otherwise untouched.

## The two grounds

The product is **light (cream)** everywhere except surfaces the owner keeps dark,
which become the manual's **reversed** treatment — cream on midnight — not gray.
Today that is `/admin` only (it applies `.dark` on its root); the mechanism stays a
`.dark` class so a future dark mode is one class away.

shadcn contract, both grounds:

| shadcn token | on cream (`:root`) | reversed (`.dark`) |
|---|---|---|
| `--background` / `--foreground` | cream / midnight | midnight / cream |
| `--card`, `--popover` (+ foreground) | cream / midnight (cards are delineated by `--border`, not a surface shift) | `color-mix(midnight 92%, cream)` / cream |
| `--primary` / `--primary-foreground` | midnight / cream | cream / midnight |
| `--secondary`, `--accent`, `--muted` | cream-deep | `color-mix(midnight 86%, cream)` |
| `--muted-foreground` | midnight-muted | `color-mix(cream 70%, midnight)` (measure ≥ 4.5:1) |
| `--destructive` / `-foreground` | err / cream | err-reversed / midnight |
| `--border`, `--input` | edge | `color-mix(cream 14%, midnight)` |
| `--ring` | bulb | bulb |
| `--chart-1..5` | ok, info, warn, err, brass | the reversed values + brass |

Focus rings are the one place the bulb appears in the product chrome: "one accent at
a time" — it is the light, and focus is where the light is.

## Fonts (§05)

- **Source Sans 3** for everything in the product that is not prose: labels, buttons,
  table cells, inputs, nav. `--font-sans`. Self-hosted through `next/font/google`
  (downloaded at build; never a runtime CDN link), weights 400/500/600 — **never below
  400**.
- **Lora** for prose and headings the user reads: `--font-serif` (already loaded).
- **Mono** stays for code and terminal output only (`--font-mono`, Geist Mono) — the
  manual lists "code" under Source Sans 3, but a proportional face in a terminal panel
  misaligns columns; this is the one recorded deviation, scoped to `<code>`/`<pre>`/
  the terminal.
- Geist Sans, Host Grotesk, DM Sans and Epilogue are removed from the app (the landing
  keeps whatever it references; if nothing does, they go).
- Font **sizes** are out of scope (the 16px body floor reflows layouts; separate pass).

## Sweep rules (every surface)

1. No colour literal outside `globals.css` and `landing.css`: no hex, `rgb()`,
   `oklch()`, `hsl()`, Tailwind palette classes (`zinc-*`, `gray-*`, `slate-*`,
   `emerald-*`, `green-*`, `red-*`, `rose-*`, `amber-*`, `yellow-*`, `orange-*`,
   `sky-*`, `blue-*`, `indigo-*`, `violet-*`, `purple-*`, `white`, `black`), or
   `[#…]` arbitrary values. Use the shadcn utilities (`bg-background`, `text-foreground`,
   `border-border`, `text-muted-foreground`, …) or `var(--token)` / `text-[var(--ok)]`.
2. Meaning-preserving mapping: green-that-meant-connected/success → `--ok`; red →
   `--err`; amber/orange → `--warn`; sky/blue/indigo → `--info`; purples and one-off
   accents → midnight or brass; near-black text → midnight; grays → the midnight
   steps; near-white → cream; drop shadows keep `rgba(15,26,43,α)` (midnight-tinted,
   the one permitted rgba) — expressed as `color-mix(in srgb, var(--midnight) N%, transparent)`.
3. State colour marks text, an icon, a border or a thin rule — **never a large fill**.
   A pill or badge is a border + text in the state colour on the ground, not a filled
   block. The `.dark` ground uses the `-reversed` values automatically via the token.
4. Never colour alone: a status must keep (or gain) its word or icon.
5. No new colour, no gradient, no tint ramp beyond the steps above.
6. Icons: lucide stays; `strokeWidth={1.5}` where a component sets it; colour follows
   text (midnight on cream, cream on midnight), brass only for rules/dividers.
7. Copy touched in passing: "Lightbulb", never "Lightbulb AI"; sentence case on buttons.

## Sign-in / sign-up

The page shells (`app/sign-in`, `app/sign-up`) move onto the tokens (their mint
`#a8e6c6`, old-green hovers and gray one-offs go). Clerk's form gets **one shared
`appearance`** (`lib/clerkAppearance.ts`) passed on `<ClerkProvider>` so every Clerk
surface (SignIn, SignUp, UserButton, OrganizationSwitcher) matches: `variables`
(`colorPrimary` midnight, `colorBackground` cream, `colorText` midnight,
`colorTextSecondary` midnight-muted, `colorInputBackground` cream-deep,
`colorInputText` midnight, `colorDanger` err, `colorSuccess` ok, `colorWarning` warn,
`borderRadius` 8px, `fontFamily` Source Sans 3) and `elements` for what variables
cannot reach (card shadow/border → edge; primary button hover → midnight-soft; footer
links → midnight). Values are the hexes, since Clerk's `variables` cannot read CSS
custom properties — recorded in one place next to the tokens they mirror.

## Admin

Stays reversed (owner keeps it dark); every `zinc-*` becomes the `.dark` contract
(`bg-background`/`bg-card`/`border-border`/`text-foreground`/`text-muted-foreground`),
every `emerald/sky/amber/red` becomes `--ok/--info/--warn/--err` (which resolve to the
reversed values under `.dark`), the header's hardcoded `zinc-800/900` becomes
`bg-card border-border`. The growth charts use `--chart-*`. `.interface-design/
system.md` is rewritten to describe this system (it currently forbids recolouring the
admin — superseded by the owner).

## The guard

A vitest test (`src/__tests__/brandTokens.test.ts`) walks `src/` (excluding
`app/landing.css`, `components/landing/**`, `app/globals.css`, and `lib/clerkAppearance.ts`)
and fails on any hex/`rgb(`/`oklch(`/`hsl(` literal or Tailwind palette class, printing
file:line. It is the reason this sweep stays done.

## Verification

Per surface, in the owner's Chrome on `localhost:3100`: sign-in, sign-up, chat (a dev
test account via the Clerk sign-in-token ticket), settings, marketplace, admin
(`admin.localhost:3100`). Contrast measured (not estimated) for body, secondary,
muted, labels and every state colour on both grounds. Then the full gate: vitest,
eslint, tsc, `next build`.

## Out of scope

Font sizes; the mobile (Expo) and desktop (Tauri) apps; blog/legal/landing design;
component structure or layout changes; dark mode for non-admin surfaces (the tokens
make it possible; shipping it is a separate decision).
