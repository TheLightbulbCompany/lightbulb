# Hero lamp in three.js — design

**Disposable.** Delete this file in the PR that ships it (CLAUDE.md, "Planning docs are disposable").

## What it is

The landing hero (`/`) enacts its own headline. The room starts dark; the pendant
lamp is off. Typing the first character of an idea — or pulling the lamp's
chain — strikes the filament and the room lights up. The idea is the lightbulb
moment; the page performs it.

The lamp is a real three.js object built to the Brand Identity Manual §02
anatomy, with a real point light inside it. The light on the wall is computed
by the renderer, not drawn as a gradient. That is the reason for three.js: the
pool of light swings with the lamp, falls off with distance, and casts the
lamp's own shadow — none of which CSS can do honestly.

Decided with the owner 2026-08-28. Supersedes the CSS-glow approach on
`feat/lightbulb-rebrand`, which stays as the fallback's light.

## Brand rules this design is checked against

- §01/§02 — the lamp is the product mark's anatomy exactly: ST64 glass, three
  tapered blades tied twice, stepped collar. No ray fan, no platform (holding
  company only). It is the mark in depth, not "bulb imagery" (§11 targets stock
  photographs and generated second bulbs).
- §06 — yellow `#F0C34E` is the light source only. It never sets type. The
  Start button is the one shape it fills, and only once the lamp is on.
- §07 — no decomposition of the mark. Nothing is broken out.
- §11 — one lamp. The 24px nav mark is the same artwork; nothing else is added.
- Open items, "Animated mark" — both states are complete stills; motion resolves
  in under two seconds; `prefers-reduced-motion` gets the static fallback.

## Stack

Versions verified against npm 2026-08-28; every peer range is satisfied by
what is already installed (react 19.2.3, three 0.184 — do NOT bump three:
`postprocessing` pins `< 0.186`).

| package | version | why |
|---|---|---|
| `three` | `^0.184.0` (already pinned, currently unused) | renderer |
| `@react-three/fiber` | `9.7.0` | declarative scene, canvas lifecycle, `frameloop="demand"` |
| `@react-three/drei` | `10.7.8` | `Environment` + `Lightformer` (glass needs an env map), `PerformanceMonitor` |
| `@react-three/postprocessing` + `postprocessing` | `3.1.1` / `6.39.x` | selective `Bloom` for the filament |

Two calls reversed by research, with the evidence:

- **Bloom is in v1.** A hot filament without bloom reads as a yellow stick.
  The pattern is selective *by construction*: `<Bloom mipmapBlur
  luminanceThreshold={1} />` and only the filament material carries
  `toneMapped={false}` with emissive > 1 — nothing else in the scene can glow.
  `mipmapBlur` is the cheap path; intensity 0.5–2.0 is the tuned range. With
  a composer in play, canvas `antialias` is off and the composer's
  `multisampling` does AA (MSAA + postprocessing double-pays).
- **drei is in v1.** Transmission materials read flat without an environment
  map — three's own docs say "always specify" one. A dark room has no HDRI,
  and drei's presets are CDN-backed (its docs say not for production), so the
  environment is built in code: `<Environment resolution={64}>` with two or
  three `<Lightformer>`s (a cool cream rect above, a dim one to the side).
  `environmentIntensity` lerps with the strike so reflections brighten with
  the room. `PerformanceMonitor` drives the quality ladder below.

**Not in v1:** drei `MeshTransmissionMaterial` (chromatic aberration, roughness
blur — costs an extra scene render per frame; the upgrade if plain
transmission reads flat).

## Components

```
components/landing/HeroLamp/
  index.tsx          next/dynamic({ ssr:false }) wrapper; picks Scene or fallback
  Scene.tsx          <Canvas> + lights + wall + <Lamp/>; frameloop="demand"
  Lamp.tsx           cord, collar, glass, filament, glow sprite, point light;
                     pendulum group; pointer + click handlers
  useLampState.ts    theme (dark|light) + persistence + strike(); the pendulum
                     integrator (Aden's stiffness/damping/scroll nudge, unchanged)
components/landing/HangingLamp.tsx   the SVG lamp — becomes the FALLBACK, unchanged
components/landing/HeroFunnel.tsx    first keystroke → strike()
app/landing.css                      DOM-side states + canvas bottom mask
```

`HeroLamp/index.tsx` chooses the fallback when: rendering on the server,
`prefers-reduced-motion: reduce`, or WebGL context creation fails. Same
triggers, same contract, instant switch.

## The scene

- **Glass** — `LatheGeometry` from the ST64 profile: straight sides rising to
  one elliptical dome. `MeshPhysicalMaterial` transmission 1, `thickness` ≈0.5
  (the property that produces refraction), roughness 0.05–0.15 (the sweet
  spot — mid-range values pixelate), IOR default, faint cream tint. Lit by
  the code-built environment above so it reads as glass in a dark room.
- **Filament** — three tapered blades (thin boxes scaled to a point) converging
  on the stem, two thin torus ties. `emissive` brass at 0.15 when off; bulb
  yellow at ≥4 when on. One additive `Sprite` (radial gradient texture, drawn
  in code) centred in the glass, opacity 0 → 1 on strike.
- **Collar** — three stepped cylinders, midnight metal. **Cord** — thin cylinder
  from above the viewport to the collar.
- **Lights** — two, split by job. A `SpotLight` inside the glass aimed at the
  wall carries the **shadow** (one shadow-map render; a shadow-casting
  `PointLight` renders six cube faces per frame — the single most expensive
  choice available here). A shadowless `PointLight` at the same spot carries
  the omnidirectional **glow** on the collar, cord and glass. Both `--bulb`,
  `decay 2`, intensity 0 → lit on strike. **Ambient** cream at 0.04 so an
  unlit room still shows the objects.
- **Wall** — one large `PlaneGeometry` behind the lamp, `MeshStandardMaterial`,
  `receiveShadow`. Colour lerps `--midnight` → `--cream` over the strike
  (~700 ms, fast attack / slow settle — the same asymmetric curve as the chat
  filament). The point light on a cream wall produces the warm centre and
  shadowed edges; the lamp casts its own shadow.
- **Pendulum** — a `<group>` pivoted at the cord anchor; `rotation.z` driven by
  the existing integrator. Scroll delta and pointer proximity add impulses.
  The light is a child of the group, so the pool swings with the lamp.
- **Colours** read from `getComputedStyle(document.documentElement)` for
  `--midnight --cream --bulb --brass` at mount. The scene cannot drift from
  `globals.css`.

## What stays DOM

Eyebrow, headline, input, Start, subtext. Lora, SEO, accessibility untouched.
They transition on `[data-lb-theme]`: headline cream 60% → ink; Start brass
outline → yellow fill; input well midnight-deeper → cream. A visually-hidden
`<button aria-label="Turn on the light">` is positioned over the glass so
keyboard and screen-reader users have the control — a canvas is not a button.

## Contract (unchanged from #2090)

`data-lb-theme="dark|light"` on `<html>`, persisted to `localStorage`
`lightbulb-theme`. **Only `.ld-page` reads it** — `/chat` is never repainted by a
marketing control. Initial state is `dark` unless a stored `light` exists.

## Performance budget

- `frameloop="demand"`: invalidate only while the pendulum has energy above a
  threshold or a strike transition is running. Settled lamp = 0 fps.
- Paused when the hero is off-screen (IntersectionObserver).
- DPR `min(devicePixelRatio, 1.5)`; spot shadow map 1024 desktop / 512 under
  640px. `PerformanceMonitor` ladder on decline: DPR → 1, then bloom off,
  then shadows off. Never the fallback — a lit lamp with no bloom is still
  the design.
- Dynamic import: three never blocks LCP; the headline paints as plain HTML.
- Canvas fades into the page background over the hero's bottom ~120px via a CSS
  mask so wall and page read as one surface.

## Testing

- Unit (vitest, jsdom — no WebGL): strike on glass click, strike on first
  keystroke (once), persistence, `data-lb-theme` write/remove, fallback chosen
  under reduced-motion. Exercised through the fallback path.
- Render (Playwright, shown to the owner before commit): dark and lit at 1440
  and 390; the pendulum settles to 0 fps (RAF count over 3 s after settle);
  LCP on `/` not regressed vs the branch head.
- Contrast: dim headline (cream 60% on midnight) ≥ 4.5:1, measured in-browser.

## Sequencing

1. The §-fixes already on `feat/lightbulb-rebrand` (fan removed, `--warn`,
   brass-as-text, lamp cord alignment, CSS glow as the fallback's light) commit
   to #2090 as-is.
2. This work lands on a branch off #2090 as its own PR, so #2090 stays
   reviewable on its own terms.

## Out of scope

`MeshTransmissionMaterial`; a 3D headline; any change to sections below the hero;
`thelightbulb.company` (the sunrise mark's showpiece belongs there, later).
