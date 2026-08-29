# thelightbulb.company — holding-company site

**Status:** design approved in-browser 2026-08-28 against the prototype at
`docs/superpowers/specs/2026-08-28-company-site-prototype.html` (open it over any
static server; it pulls three + GSAP from jsdelivr). The prototype IS the visual
spec — this document records the decisions and the parts that have to change when
it becomes a real app. Delete both when the site ships.

## What it is

The landing page for **The Lightbulb Company**, the holding company, at
`thelightbulb.company`. One page. Its job: make the vision legible — one bulb,
many rooms — and point at the products. Tagline: **"The business of ideas."**

Products, in scroll order, with their status tags:

| # | Product | tag | the idea inside the bulb |
|---|---|---|---|
| 02 | Lightbulb AI → flickmeon.ai | Live | a network of agents |
| 03 | GooseTown | Building | a little skyline |
| 04 | Ventures | Next | a compounding bar chart |
| 05 | Physical AI | Next | a gear |

## Decisions (do not re-litigate)

- **Dark throughout.** No light/paper flip — it was tried and rejected as
  "weird and too bright". The strike warms the ground from `#0a0a0a` to
  `#1a1612`; type stays cream. Gold (`#f0c34e`, the logo's bulb colour) is the
  only accent.
- **Distinct identity, shared logo.** Only the lamp mark comes across from the
  Lightbulb AI brand manual (`apps/frontend/src/components/Logo.tsx` — the glass
  path, three-blade filament, collar, and the 53-ray `SunriseMark` for the
  footer). Nothing else: not the palette, not the fonts, not the tokens.
  Fonts: Instrument Serif (display), Inter (body), JetBrains Mono (labels).
- **The bulb stays whole.** An exploded-view version was built (`v1-explode`)
  and rejected. The bulb stands on a floor with a reflection, turns slowly, and
  **what's inside morphs**: one 24k-point cloud that is scattered dust in the
  dark hero → converges into a brain on the strike → one shape per product →
  back to the brain for the vision chapter, where the bulb sinks under the copy.
- **No ring light, no little person, no pixel bulb.** Considered, declined.
- **The timeline is read off the page, not hand-tuned.** Each chapter's shape is
  fully formed while its `<section>` fills the viewport and morphs in the gap
  between sections (`layout()` in the prototype derives every window from
  `offsetTop / (scrollHeight − innerHeight)`, re-run on resize and font load).
  This is what "transitions in the right place" means; keep it when porting.

## Where it lives

`apps/company` — a new Next.js 16 app (`@lightbulb/company`) in this monorepo,
its own Vercel project, domain `thelightbulb.company` + `www`. No Clerk, no
middleware, no `packages/shared`. Reuses the already-installed `three`,
`@react-three/fiber`, `@react-three/drei`, `@react-three/postprocessing`, `gsap`.
Nothing new in `package.json` beyond what the frontend already pins.

DNS: the `thelightbulb.company` zone is CDK-managed and carries mail (Google
Workspace + SES). The site needs only an apex `A` + `www` `CNAME` to Vercel; MX
is untouched. The product's Vercel records are not in CDK (no `76.76.21.21`
anywhere in `apps/infra`), so these follow the same path: added when the Vercel
project is created, recorded in the PR description.

Deploy: `deploy.yml` is generated from `apps/infra/lib/app.ts` and its frontend
steps are `vercel build --prebuilt` per env. The company site gets a **dev
preview + prod** pair of steps the same way, prod gated like every other prod
job (`workflow_dispatch` only). `cdk synth` + commit the regenerated
`deploy.yml` in the same PR.

## Porting notes — what changes from the prototype

- **R3F, not raw three.** `<Canvas frameloop="demand">` + `invalidate()` from the
  scroll handler and while a morph/tween is mid-flight, like `HeroLamp/Scene.tsx`.
  The prototype runs an unconditional rAF loop; the app must idle at 0 fps.
- **Quality ladder** copied from `HeroLamp`: DPR 1.5 → 1, then bloom off. Point
  count halves under 640px.
- **`prefers-reduced-motion`**: the page sits at the *strike* state (lit,
  assembled brain), no morphs, no drift. All copy is HTML and reads without JS.
- **Point morphing** stays a single `Float32Array` lerp on the CPU (24k × 3 per
  frame is nothing); no shader unless profiling says so.
- **Leader label**: one element, projected from `bulb.localToWorld` each frame —
  keep it in the R3F `useFrame`, not React state.
- **Mobile (< 800px)**: bulb centred behind the text, chapters stack, rail /
  scrub / label hidden (already in the prototype CSS).
- **Copy** is placeholder throughout except the tagline; product blurbs and the
  vision paragraph are the owner's to rewrite.

## Testing

- One vitest per shape generator: point count, bounding radius, and the brain's
  sagittal gap (no point with `|x| < 0.028`).
- One vitest for `layout()`: given section offsets, each shape's hold window
  contains its section's top.
- Playwright screenshot at each chapter top (dev preview) for the visual
  self-review before the PR; not a CI gate.

## Out of scope

CMS, blog, i18n, analytics, per-product pages, a light theme.
