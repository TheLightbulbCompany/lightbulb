# Hero room v2 — addendum to the hero-lamp spec

**Disposable.** Delete with the parent spec when this ships. Parent:
`docs/superpowers/specs/2026-08-28-hero-lamp-three-design.md` (still binding where not amended here).

Decided with the owner 2026-08-28 after seeing Tasks 1–9 live.

## What changes

The hero is a **room with a desk**. The pendant lamp hangs over a desk with a
laptop on it. The room starts dark, the laptop closed. The first character
typed into the idea box opens the laptop's lid, wakes its screen — which shows
the idea being typed — and strikes the lamp; the room lights. The lamp also
**follows the cursor**: the cord's anchor slides along the top to the pointer's
x and the cord pays out toward the pointer's y, so the visitor shines the lamp
over the desk and the light pool goes where they point.

## Owner rulings recorded here

1. **The laptop is Ksenia Kondrashova's MacBook model, copied whole, logo
   included** — owner: "copy everything including the logo, it's fine for now."
   Source: her CodePen `gOEgyaj` (code MIT under CodePen's terms). The GLB is
   her personal asset with no stated license and an Apple-logo mesh. It is
   **self-hosted** at `apps/frontend/public/models/laptop/mac-noUv.glb` (642 KB)
   with `keyboard-overlay.png` (75 KB) — never hotlinked. Materials are
   assigned by mesh NAME (`lid logo screen-frame camera base inner keyboard
   legs`, groups `_top` / `_bottom`), so swapping the model later is a model
   change only. Cost if the ruling is reversed: replace the GLB; nothing else.
2. **The lamp does not cast its own shadow on the wall.** With one shadow spot
   inside the glass pointing down, that is geometrically impossible; the honest
   shadows in the room are the desk's and the laptop's. Parent spec amended.
3. **Aden's three notes are one bug plus one.** "Colors are off" / "the white
   box in the lit version" / "that gray is meh": the canvas wall receives
   environment + ambient light on top of its exact page-colour emissive, so it
   is a lighter rectangle when lit and a greyer one when dark; the dim headline
   (cream at 62%) reads flat grey; and the nav keeps a hardcoded old-cream
   background (`rgba(250,247,242,.95)`, globals.css `.landing-nav`) with no dark
   rule, so it floats cream over the midnight room.

## The room — geometry in scene units (camera at `[0, 0.4, 8]`, fov 40, wall at z = −1.6)

Visible half-height at z = 0 is `8·tan(20°) ≈ 2.91`, so world y runs ≈ −2.5 … 3.3.

- **Desk** — a box `[8, 0.12, 4.1]` whose top face is at **y = −1.5**, spanning
  z = −1.6 (against the wall) to z = 2.5 (front edge, toward the camera). Matte
  midnight (`MeshStandardMaterial`, roughness 0.92, `receiveShadow`). A brass
  hairline along the front top edge: a box `[8, 0.012, 0.012]` in brass.
  Because the camera sits 1.9 above the desk looking level, the top reads as a
  receding band in the lower ~18% of the frame; its front edge projects near
  the DOM input.
- **Laptop** — the GLB, scale **0.055** (her screen plane is 29.4 units wide →
  ≈ 1.6 here), placed on the desk at `[0.55, −1.5, 0.4]`, yawed −0.18 rad so
  the lid faces the camera slightly from the right; `_top` is the lid group,
  hinged at the model origin as in her setup. `castShadow` on every mesh,
  `receiveShadow` on `base`.
  - Materials, copied from her code (owner ruling 1): `lid`/`base` →
    `MeshStandardMaterial 0xCECFD3`; `screen-frame`/`legs`/`keyboard`/`inner`
    → dark plastic `0x000000 roughness .9 metalness .9`; `camera` → basic
    `0x333333`; `logo` → basic `0xffffff`. These are the ONE place hex colours
    are allowed in the scene, because they are hers, not ours — a comment says
    so.
  - **Screen** — a `PlaneGeometry(29.4, 20)` in lid space at `[0, 10.5, −0.11]`
    rotated `[π, 0, 0]` (hers), `MeshBasicMaterial` with our **idea texture**
    (below), `toneMapped: false`, opacity 0 → 0.96 on wake; a dark plane 1 mm
    behind it for the off state; a `RectAreaLight(cream, 0 → 1.5, 29.4, 20)`
    at the same pose so the screen lights the keyboard and desk when it wakes
    (`RectAreaLightUniformsLib.init()` once, from `three/examples/jsm`).
  - **Keyboard overlay** — her `keyboard-overlay.png` as an `alphaMap` on a
    `PlaneGeometry(27.7, 11.6)` at `[0, .045, 7.21]` rotated `[−π/2, 0, 0]` in
    `_bottom` space, `MeshBasicMaterial white, transparent`.
- **Idea texture** — a `CanvasTexture` 1024×700, redrawn ONLY when the idea
  text changes: midnight fill, a cream caret, the text in `500 56px Lora` (the
  page loads Lora via next/font; draw after `document.fonts.ready`), wrapped to
  the width, cream. Empty idea = caret only. The text comes from a new store
  field `ideaText` that `HeroFunnel` writes on every change.
- **Choreography** (her `mainTl`, bound to our strike tween `k ∈ [0,1]`):
  - `k = 0`: lid closed — `_top.rotation.x = 0.5π`, screen off, `RectAreaLight`
    0.
  - The lid opens over the first 80% of the tween, `0.5π → −0.2π`, with the
    small `z += 0.5·(1−k')` nudge she uses; the screen wakes at `k = 0.05`
    (opacity 0 → 0.96 over 0.06 of the tween, light 0 → 1.5).
  - Her `laptopAppearTl` (rise from below while rotating flat → tilted) plays
    ONCE on first mount of the scene over 1.5 s, independent of theme, ending
    seated on the desk. Returning visitors with a stored `light` see it too —
    it is the room appearing, not the strike.
  - No floating loop (would defeat the 0-fps demand loop). No OrbitControls,
    no webcam, no lil-gui.
- **Lamp follows the cursor** — in `Scene`'s `Driver`: pointer position over
  the hero maps to a target anchor x (`(clientX/innerWidth − 0.5) · 2 ·
  halfWidth`, clamped ±(halfWidth − 0.6)) and a target glass y (pointer y from
  the hero's top to 70% of its height → glass centre y from 1.7 down to a
  floor of **0.2**, so the glass bottom stays ≥ 0.35 above the laptop lid's
  top). Both spring toward their targets (`v += (target − x)·0.012; v *= 0.86`)
  and the pendulum receives an impulse ∝ anchor x velocity (`−vx · 0.9`,
  clamped by `IMPULSE_CLAMP`). Cord length = anchor y − collar top, so the
  cord pays out. Pointer leaves the hero → targets return to rest (x 0,
  y 1.7). Touch: `pointermove` from a touch drag counts; no pointer = rest.
  The demand loop invalidates while any spring is above threshold.
  `Lamp` gains `anchorRef: MutableRefObject<{x: number; y: number}>`; its
  group position and cord length read it each frame.
- **The switch follows the glass.** `Scene` receives `switchRef` (the
  `.ld-lamp-switch` button) and, on every rendered frame where the lamp moved,
  projects the glass centre to canvas pixels and writes
  `style.transform = translate(px, py)` (the button's CSS `top`/`left` become
  0 / 50% with a −50% margin; size unchanged). Keyboard focus ring stays where
  the bulb is.

## Colour fixes (ruling 3)

- Wall `envMapIntensity 0`; ambient intensity `0.012` (just enough that the
  desk/laptop silhouettes exist in the dark). The wall's base is then EXACTLY
  the page colour in both states; only the lamp pool and the screen add to it.
- Pool light colour = `bulb.lerp(cream, 0.4)` (warm, not yellow-green) for
  both the spot and the point.
- Seam measurement (Task 14): with `preserveDrawingBuffer: true` set
  TEMPORARILY, read a wall pixel 200 px above the canvas bottom, well away
  from the pool, and compare with `getComputedStyle(.ld-page).backgroundColor`
  in both states: every channel within 3/255. Remove the flag after.
- Dark headline: `rgba(245, 239, 226, 0.92)` (the manual's reversed treatment
  is cream on midnight; "waiting" is conveyed by the unlit lamp, not grey type).
- Nav: `[data-lb-theme="dark"] .landing-nav { background: rgba(15,26,43,.92);
  border-bottom-color: rgba(245,239,226,.14) }` and its links/badge to cream
  tones; the light-mode background moves from the old `rgba(250,247,242,.95)`
  to `rgba(245,239,226,.95)` (the current cream). Both transition with the
  page-wide rule (the nav is inside `.ld-page`; if it is not, scope the rule
  to the attribute alone and say so in the report).

## Brand rules re-checked

One lamp (unchanged). Yellow is light only (the screen is cream-on-midnight;
the pool is bulb-toward-cream). The laptop's hex materials are the owner's
explicit exception. The idea texture uses Lora (§05). Nothing rotates
continuously; the 0-fps contract holds — the only new per-frame work is while
the cursor moves the lamp or the strike/appear tweens run.

## Store contract (extended)

`lampStore.ts` adds `ideaText: string`, `setIdeaText(t)`, `useIdeaText()`;
`subscribe` notifies on either field. `HeroFunnel` calls `setIdeaText(next)`
in the same `onChange` that calls `lightOn()`.

## Performance

GLB 642 KB + PNG 75 KB load through drei's `useGLTF`/`useTexture` inside the
already-dynamic scene chunk — never on `/`'s first load. `useGLTF.preload`
is called from `Scene` so the model starts fetching as soon as the chunk runs.
Shadow casters grow from 4 to ~12 meshes; still one shadow map. The idea
texture redraw is one 2D-canvas paint per keystroke. The switch projection is
one `Vector3.project` per moved frame.

## Out of scope

Her webcam feed; her scrolling screenshot texture (deleted from the assets —
the screen shows the visitor's idea); the floating bob; a stylised laptop
(future swap); the org-chart / globe ideas (dropped).
