# App Brand Tokens — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every colour and typeface in the product resolves to the Brand Identity Manual's tokens; shadcn stays as components with its palette replaced; admin becomes the manual's reversed (cream-on-midnight) treatment; sign-in/up match; a test keeps it that way.

**Architecture:** Task 1 rewrites the token layer in `globals.css` (+ fonts in `layout.tsx`) and renames every old token reference repo-wide, so the app compiles and renders on the new tokens before any component is touched. Tasks 2–7 then sweep disjoint directories in parallel (no shared files), replacing literals and Tailwind palette classes with tokens. Task 8 adds the Clerk appearance + auth shells, Task 9 the guard test + docs, Task 10 verifies in the browser and runs the full gate.

**Tech Stack:** Next.js 16, React 19, Tailwind v4 (`@theme inline` in `globals.css`, no config file), shadcn/ui, Clerk (`@clerk/nextjs`), `next/font/google`, vitest.

**Spec:** `docs/superpowers/specs/2026-08-28-app-brand-tokens.md` — the token table, the two-ground contract and the sweep rules there are binding; this plan does not restate values it can reference.

**Worktree / branch:** `.worktrees/brand-visual`, branch `feat/app-brand-tokens` (off `feat/hero-lamp-three`). Paths relative to `apps/frontend/` unless noted. `grep` on this machine is ugrep — use `git grep` for reliable regex.

## Global Constraints

- The only colours the product may use are the spec's token table; the only fonts are Source Sans 3 (UI), Lora (prose), and mono for code/terminal only. No new colour, no gradient, no tint ramp beyond the listed steps. Font sizes are out of scope.
- No colour literal outside `app/globals.css`, `app/landing.css`, and `lib/clerkAppearance.ts`: no hex, `rgb()`/`rgba()`/`oklch()`/`hsl()`, no Tailwind palette class (`zinc gray slate neutral stone emerald green red rose amber yellow orange sky blue indigo violet purple white black` + `-N`), no `[#…]` arbitrary colour. Midnight-tinted shadows are `color-mix(in srgb, var(--midnight) N%, transparent)`.
- Meaning-preserving: success → `--ok`, error → `--err`, warning → `--warn`, info → `--info` (the `.dark` ground swaps to `-reversed` through the token — never reference a `-reversed` token directly in a component); accents → midnight/brass; text → midnight steps; grounds → cream steps. State colour never fills a large area; never colour alone.
- Retired tokens are deleted, not aliased. Renames are at the call site.
- Sweep tasks (2–7) touch ONLY their listed directories; they do not edit `globals.css`, `layout.tsx`, or each other's files. Implementers do NOT commit — the controller commits per task after review. Stage nothing.
- Every task ends with: `pnpm eslint <touched dirs>` 0 errors, `pnpm tsc --noEmit` 0 errors, `pnpm vitest run <touched dirs>` green, and the file:line list from the guard script (`node scripts/brand-tokens-scan.mjs <dir>`, created in Task 1) EMPTY for its directories.
- Copy touched in passing: "Lightbulb", never "Lightbulb AI"; sentence case on buttons.

---

### Task 1: The token layer, the fonts, and the rename

**Files:**
- Modify: `src/app/globals.css` (`@theme inline`, `:root`, `.dark`, base layer fonts)
- Modify: `src/app/layout.tsx` (fonts)
- Modify: every file referencing a retired token (rename sweep — ~211 files incl. `landing.css` and `components/landing/**`)
- Create: `scripts/brand-tokens-scan.mjs` (the scanner the guard test and every sweep task use)
- Modify: `src/components/landing/HeroLamp/lampGeometry.ts` / `Scene.tsx` only if they read a renamed token by name (`cssColor("--…")`)

**Interfaces (produces):** the token names in the spec table; `--font-sans` (Source Sans 3), `--font-serif` (Lora), `--font-mono`; Tailwind utilities `text-[var(--ok)]` etc. work because `@theme inline` exposes `--color-ok: var(--ok)`, `--color-warn`, `--color-err`, `--color-info`, `--color-brass`, `--color-bulb`, `--color-midnight`, `--color-cream`, `--color-cream-deep`, `--color-cream-deeper`, `--color-edge` so `bg-cream-deep`, `text-ok`, `border-edge`, `text-brass` are valid utilities.

- [ ] **Step 1: The scanner.** Create `scripts/brand-tokens-scan.mjs` (plain Node, no deps): walks the given paths (default `src`), skips `app/globals.css`, `app/landing.css`, `lib/clerkAppearance.ts`, `components/landing/**`, `**/__tests__/**`, `*.test.*`, and prints `file:line: <match>` for every: hex colour `#([0-9a-fA-F]{3}){1,2}\b` **not** inside a comment line (`//`, `/*`, `*`) and not followed by an identifier char; `\b(rgba?|oklch|hsla?)\(`; Tailwind palette classes `\b(bg|text|border|ring|from|to|via|fill|stroke|shadow|outline|decoration|divide|placeholder|caret|accent)-(zinc|gray|slate|neutral|stone|emerald|green|red|rose|amber|yellow|orange|sky|blue|indigo|violet|purple|pink|fuchsia|cyan|teal|lime)-\d{2,3}\b` and `\b(bg|text|border)-(white|black)\b`; arbitrary `\[#[0-9a-fA-F]{3,8}\]`. Exit code 1 if any match. Run it on `src` now and save the count (this is the baseline the sweeps burn down; expect ~1,700).
- [ ] **Step 2: Fonts.** `layout.tsx`: import `Source_Sans_3` from `next/font/google` (`variable: "--font-source-sans"`, weights `["400","500","600"]`, `subsets: ["latin"]`, `display: "swap"`), keep `Lora`; remove `Geist`, `Host_Grotesk`, `DM_Sans`, `Epilogue` **only if** nothing outside the landing references their variables (`git grep -n "font-geist-sans\|font-host\|font-dm\|font-epilogue" -- src` — the landing may keep some; whatever is still referenced stays loaded). Keep `Geist_Mono`. The `<html>` className carries the font variables. `globals.css` `@theme inline`: `--font-sans: var(--font-source-sans)`, `--font-serif: var(--font-lora-serif)`, `--font-mono: var(--font-geist-mono)`; delete `--font-host`, `--font-dm`, `--font-epilogue`, `--font-lora` aliases and repoint their users in `globals.css` (`var(--font-dm)` → `var(--font-sans)`, `var(--font-lora)` → `var(--font-serif)`). The `body` rule sets `font-family: var(--font-sans)`. Headings that were Lora stay Lora (`var(--font-serif)`).
- [ ] **Step 3: `:root` tokens.** Replace the palette block with exactly the spec table (values verbatim; keep the manual-citing comments short — one line per token, no essays). Replace the shadcn contract with the spec's "on cream" column. Add the chart tokens. Add the `@theme inline` colour exports listed under Interfaces.
- [ ] **Step 4: `.dark` (reversed).** Replace the oklch block with the spec's "reversed" column, plus `--ok: var(--ok-reversed)` (and warn/err/info) so components never name a `-reversed` token. Keep the block's scoping comment to two lines.
- [ ] **Step 5: The rename.** Using `git grep -l` then `sed -i ''` (macOS), rename across `src/` (landing included): `--forest-tint`→`--cream-deep`, `--forest-soft`→`--brass`, `--edge-forest`→`--edge`, `--forest`→`--midnight`, `--gold`→`--brass`, `--ink-soft`→`--midnight-soft`, `--ink`→`--midnight`, `--mute-soft`→`--midnight-faint`, `--mute`→`--midnight-muted`, `--lamp-on`→`--bulb`, `--lamp-dim`→`--brass`, `--lamp-out`→`--midnight-faint`, `--danger`→`--err`. Order matters (longer names first). Then `git grep -n "\-\-forest\|\-\-gold\|\-\-ink\|\-\-mute\|\-\-lamp-\|\-\-danger"` must return nothing outside comments. Check the landing dark block in `landing.css` (~line 2542) that redefines `--cream`/`--ink`/`--forest` inline for the dark room — after the rename it redefines `--midnight`/`--cream`; make sure it still reads as intended (the room swaps ground and type). Check `HeroLamp/Scene.tsx`'s `readColors`/`cssColor` calls and `lampGeometry.test.ts` for token names.
- [ ] **Step 6: Contrast.** With the dev server on `:3100`, on any cream page, compute in the console (or a tiny script) the contrast of `--midnight-soft`, `--midnight-muted`, `--midnight-label`, `--ok`, `--warn`, `--err`, `--info` on `--cream`, and on `admin.localhost:3100` (reversed) `--muted-foreground` and the four `-reversed` values on `--midnight`. Record the numbers in a comment block at the top of the palette; every text token must be ≥ 4.5:1 (labels ≥ 4.5 too — they are text). If `--midnight-muted` at 64% is under 4.5, raise the percentage until it clears and note it.
- [ ] **Step 7: Verify + hand back.** `pnpm vitest run` (full — the rename touches tests), `pnpm eslint src` 0 errors, `pnpm tsc --noEmit`, `pnpm build` EXIT 0. The scanner's count for `src` is now the baseline minus nothing (this task adds no literals; it may remove a few). Do not commit; report.

---

### Task 2: Chat + workspace

**Files:** `src/components/chat/**` (incl. `ChatLayout.css`, `FileViewer.css`), `src/components/workspace/**` (incl. `workspace.css` — also fix the stale forest/cream comment at lines ~20–21), `src/app/chat/**`.

- [ ] **Step 1:** `node scripts/brand-tokens-scan.mjs src/components/chat src/components/workspace src/app/chat` → the worklist (~390 sites). Read each file's surrounding intent before replacing: the greens (`#2d8a4e`, `#0a5c3e`, `#1a5c32`, `#c8e6c9`, `#e8f5e9`) mark *connected/success* → `--ok` for text/icon/border and NO fill (a "connected" pill becomes `border-[var(--ok)] text-[var(--ok)]` on the ground); `#5b5b5b`/`#5a5549`/`#6b6b6b` → `--midnight-muted`; `#b5ae9e` → `--midnight-faint` (non-text) or `--midnight-muted` (text); `#e8e3d9`/`#f3eee5` → `--cream-deep`; near-white → `--cream`; `#8b8bf5`/`#6336e7`/`#3859ff` accents → `--midnight` (or `--info` where they mark information); reds → `--err`; ambers → `--warn`; shadows → the midnight color-mix.
- [ ] **Step 2:** Replace, file by file, keeping class order tidy; where a Tailwind arbitrary value is replaced by a token utility use the exported names (`bg-cream-deep`, `text-ok`, `border-edge`, `text-midnight-muted` — add any missing export to your report rather than editing `globals.css`).
- [ ] **Step 3:** Scanner for the three dirs → EMPTY. `pnpm vitest run src/components/chat src/components/workspace src/app/chat`, eslint, tsc. Render check on `:3100/chat` is Task 10's; you may spot-check with Playwright if a signed-in session exists, otherwise skip.

---

### Task 3: Marketplace

**Files:** `src/components/marketplace/**`, `src/app/marketplace/**`, `src/app/m/**` (if present — `find src/app -path "*market*"`).

- [ ] Same three steps as Task 2 on ~460 sites. Specifics: `#8a3a2a`/`#fbe9e7` (error tint) → `--err` text/border, no fill; `#0a5a3d` → `--ok`; `#f3eee5` → `--cream-deep`; listing cards keep their layout, lose their tints. Price/paid badges: `--brass` border + text (a "detail colour"), never bulb.

---

### Task 4: Control, channels, onboarding, getstarted, ui

**Files:** `src/components/control/**`, `src/components/channels/**`, `src/components/onboarding/**`, `src/components/getstarted/**`, `src/components/ui/**`, `src/app/getstarted/**`, `src/app/onboarding/**`.

- [ ] Same three steps (~170 sites). In `components/ui/*` the shadcn primitives may carry `dark:` variants with stock classes — replace with token utilities that already switch under `.dark` via the contract, and delete redundant `dark:` variants. `sonner.tsx` keeps its light-only stance. Status dots in `getstarted`: `--ok`/`--warn`/`--err` with the word beside them.

---

### Task 5: Settings, desktop, teams, support, notifications, agents

**Files:** `src/components/settings/**`, `src/app/settings/**` (incl. `settings.css`), `src/components/desktop/**`, `src/app/auth/desktop-callback/**`, `src/components/teams/**`, `src/components/support/**`, `src/components/notifications/**`, `src/components/agents/**`.

- [ ] Same three steps (~120 sites). `#1c1917` (stone-900 text) → `--midnight`; `#dc2626` → `--err`; the stray `zinc` in settings → the contract; Google-connection greens in `ConnectionsList` → `--ok` text/icon only (the Google logo SVG keeps its own brand colours — that is a third party's mark, exempt; put its colours in `src/components/settings/googleBrand.ts` as named constants with a one-line comment, and add that file to the scanner's skip list in your report for Task 9 to adopt).

---

### Task 6: Admin

**Files:** `src/app/admin/**`, `src/components/admin/**`, `.interface-design/system.md` (repo-level — rewrite).

- [ ] **Step 1:** Scanner on the two dirs → ~513 Tailwind classes. Mapping under `.dark`: `bg-zinc-950/900` → `bg-background`; `bg-zinc-900/800` panels → `bg-card`; `border-zinc-800/700` → `border-border`; `text-zinc-100/200` → `text-foreground`; `text-zinc-300/400/500` → `text-muted-foreground`; `hover:bg-zinc-800` → `hover:bg-accent`; `text-emerald-*` → `text-ok`; `text-sky-*` → `text-info`; `text-amber-*`/`bg-amber-500` → `text-warn` / `border-warn` (no fill — a warning banner is `border-l-2 border-warn` on `bg-card`); `text-red-*` → `text-err`; `bg-red-*` fills → border + text. The header's hardcoded `zinc-800/900` → `bg-card border-b border-border`.
- [ ] **Step 2:** Growth charts (`app/admin/growth/**`, `components/ui/chart.tsx` consumers): series colours through `var(--chart-1..5)` only.
- [ ] **Step 3:** Rewrite `.interface-design/system.md` (≤ 40 lines): the token set, the two grounds, the state pairs, fonts, the "no literal" rule and the scanner command. It currently says the admin stays un-recoloured — that is superseded.
- [ ] **Step 4:** Scanner EMPTY for the two dirs; vitest for `src/app/admin src/components/admin`; eslint; tsc. Render check: `http://admin.localhost:3100/admin` (the admin host allowlist includes `admin.localhost:3000`; check `middleware.ts` / `NEXT_PUBLIC_ADMIN_HOSTS` for the `:3100` port — if the host check is port-insensitive it just works; if not, note it for Task 10 rather than changing middleware).

---

### Task 7: Remaining `src/app/**` and `src/lib`/`src/hooks` strays

**Files:** everything the scanner still lists under `src/app/**` after Tasks 2–6's directories are excluded (blog/legal/landing are exempt), plus `src/lib/**`, `src/hooks/**`, `src/components/*.tsx` (top-level components like `Logo.tsx`, `DesktopAuthListener.tsx`), `src/middleware.ts`.

- [ ] Same three steps. `Logo.tsx` is already tokenised — verify the rename left it correct. Do NOT touch `app/sign-in`/`app/sign-up` (Task 8).

---

### Task 8: Sign-in / sign-up + one Clerk appearance

**Files:**
- Create: `src/lib/clerkAppearance.ts`
- Modify: `src/app/layout.tsx` (`<ClerkProvider appearance={clerkAppearance}>`) — coordinate: Task 1 also edits `layout.tsx`; this task runs AFTER Task 1 lands and edits only the provider line.
- Modify: `src/app/sign-in/[[...sign-in]]/page.tsx`, `src/app/sign-up/[[...sign-up]]/page.tsx`
- Test: `src/lib/__tests__/clerkAppearance.test.ts`

- [ ] **Step 1 (test first):** the test imports `clerkAppearance` and asserts `variables.colorPrimary === "#0F1A2B"`, `variables.colorBackground === "#F5EFE2"`, `variables.colorDanger === "#96382B"`, `variables.fontFamily` contains `Source Sans`, and that every string value in `variables` that starts with `#` is one of the spec's token hexes (list them in the test). Run → fails (module missing).
- [ ] **Step 2:** `clerkAppearance.ts` exports `const clerkAppearance: Appearance` (type from `@clerk/types` or `@clerk/nextjs`): `variables` per the spec (midnight/cream/midnight-muted computed hex — compute `color-mix` results once and write the hex with a comment giving the mix), `borderRadius: "8px"`, `fontFamily: "var(--font-sans), 'Source Sans 3', system-ui, sans-serif"`, `fontFamilyButtons` same; `elements`: `card: "shadow-none border border-edge bg-cream"`, `formButtonPrimary: "bg-midnight text-cream hover:bg-midnight-soft"`, `footerActionLink: "text-midnight underline"`, `formFieldInput: "bg-cream-deep border-edge text-midnight"`, `headerTitle: "font-serif"` (Lora for the one heading), `socialButtonsBlockButton: "border-edge bg-cream text-midnight hover:bg-cream-deep"`. Clerk cannot read CSS vars in `variables`, so the hexes live here — this file is on the scanner's skip list by design; keep it the only such file.
- [ ] **Step 3:** `layout.tsx`: `<ClerkProvider appearance={clerkAppearance}>`. Remove the two `avatarBox`/`rootBox` layout-only `appearance` props elsewhere only if the shared one covers them (it does not — leave them).
- [ ] **Step 4:** The two page shells: replace `#a8e6c6`, `#0a5c3e`, `#054d33`, `#74716a`, `#6b6960`, `#9e9a90`, `rgba(74,158,116,.15)`, `rgba(6,64,43,.03)`, `rgba(255,255,255,.55)` with tokens (`--bulb` is allowed ONCE per page as the light accent on the brand panel — a thin brass rule is the safer default; the mint blur becomes a midnight-tinted `color-mix`). Keep the split layout. Scanner EMPTY for the two dirs.
- [ ] **Step 5:** Render `/sign-in` and `/sign-up` on `:3100` at 1440×900 and 390×844 (Playwright): the Clerk card is cream with edge borders, the primary button midnight, inputs cream-deep, errors in `--err` (trigger one by submitting empty). Screenshots to `.playwright-mcp/auth-signin.png`, `auth-signup.png`, `auth-signin-390.png`. Tests, eslint, tsc.

---

### Task 9: The guard test + docs

**Files:**
- Create: `src/__tests__/brandTokens.test.ts` — imports the scanner's `scan(paths, {skip})` function (refactor `scripts/brand-tokens-scan.mjs` to export it and keep its CLI), runs it on `src` with the skip list (globals.css, landing.css, `components/landing/**`, `lib/clerkAppearance.ts`, `components/settings/googleBrand.ts`, tests), and asserts the result is empty, printing every `file:line` on failure.
- Modify: `CLAUDE.md` (repo root) — in the Frontend section, five lines: the token set lives in `globals.css`; no colour literals outside the three exempt files; the guard test; the scanner command; fonts.
- Delete: this plan and its spec (the PR description is the record).

- [ ] Steps: write the test (expect green after Tasks 2–8); if it lists strays, fix them in place (they are yours now); `pnpm vitest run src/__tests__/brandTokens.test.ts`; docs; delete the two planning docs.

---

### Task 10: Look at it, then the full gate

- [ ] In the owner's Chrome (Claude-in-Chrome, tab on `localhost:3100`) at 1440×900: `/sign-in`, `/sign-up`, `/settings`, `/marketplace`, `/chat` (sign in with a dev test account via the Clerk sign-in-token ticket — see CLAUDE.md "Manual dev testing accounts"; if no Clerk secret is reachable, `/chat`'s unauthenticated redirect is what you can verify and you say so), `http://admin.localhost:3100/admin` (reversed). READ every screenshot: no green, no gray-on-gray, no stock red; text hierarchy visible; state colours carry a word/icon; buttons midnight-on-cream (cream-on-midnight in admin); focus ring bulb. Save under `.playwright-mcp/tokens-*.png`.
- [ ] Measure contrast on each surface for body, secondary, muted, label and each state colour actually rendered (script in the console: sample computed colours of real elements) — record in the PR body.
- [ ] Full gate: `pnpm vitest run` (all), `pnpm eslint src` (0 errors), `pnpm tsc --noEmit`, `pnpm build` EXIT 0. Fix anything red; then the branch is ready for its PR against `feat/hero-lamp-three`.

## Self-review

- Spec coverage: tokens/grounds/fonts (T1), every surface (T2–T7), auth + Clerk (T8), guard + docs (T9), verification + gate (T10). Admin's `system.md` (T6). Charts (T6). Rename incl. landing (T1). Google logo exemption (T5 → T9 skip list).
- Placeholders: none — mappings are explicit; counts are estimates from the survey and marked so.
- Types/names: token names identical across spec, T1 exports, and the sweeps' utility names (`bg-cream-deep`, `text-ok`, `text-warn`, `text-err`, `text-info`, `border-edge`, `text-brass`, `text-midnight-muted`, `text-midnight-soft`); `clearTheming`-style naming not involved here; `scan(paths, {skip})` shared between T1's CLI and T9's test.
