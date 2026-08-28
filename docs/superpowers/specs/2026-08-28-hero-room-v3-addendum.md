# Hero room v3 — the hero on the laptop

**Disposable.** Delete in the PR that ships it. Amends the (already-deleted, see git
history at `c8c691a29^`) v1 spec and v2 addendum; where silent, they still bind.

Decided with the owner 2026-08-28 after seeing v2 live: *"I thought the laptop would
be much bigger and the whole hero would sit in the laptop, and it would never close."*

## What changes

The laptop is the stage. It fills the hero (screen ≈ 60–70% of the viewport width at
1440), lid **always open**, and the hero's copy — eyebrow, headline, the idea input,
Start, the fine print and the swap link — is rendered **on its screen** as real DOM
in perspective. The room, the lamp, the strike-on-first-keystroke, the cursor-follow,
the page-wide theme transition and the 0-fps contract are unchanged. The lid
choreography and the code-drawn idea texture are removed: the visitor types *on* the
laptop.

## Composition (scene units; tuned by render in Task 17, starting values here)

- Camera pulled in and raised: start `[0, 1.2, 7.5]`, fov 45, looking at
  `[0, 0.1, -0.6]`. Constraints that bind, at 1440×900 AND 1280×720: screen width
  60–70% of the viewport; the lamp's glass fully visible above the screen's top edge
  with ≥ 0.3 world units clearance at rest; the desk's front edge visible below the
  base; no copy clipped.
- Laptop: start scale 0.17 at `[0, DESK_TOP_Y, 0.2]`, yaw 0 (face-on) — a slight yaw
  (≤ 0.06 rad) is allowed if it reads better. Lid fixed at `LID_OPEN = −0.2π`. The
  one-time rise-in on load stays (already open). Materials as before (owner's hex
  exception). `castShadow` as before.
- Lamp: `LAMP_ANCHOR_Y` and `GLASS_CENTER_Y` rise so the bulb hangs clear of the lid
  (start 3.6 / 2.5); the cursor-follow's glass-y floor becomes `GLASS_CENTER_Y − 0.4`
  (the lamp moves mostly sideways now; it must never enter the screen).
- Desk: unchanged (W 13, D 2.0, top −1.5); the laptop's base sits on it.
- Screen: a midnight emissive plane (the "glass") behind the DOM, always on — cream
  type on midnight is the manual's reversed treatment, so the screen looks the same in
  both rooms; the `RectAreaLight` stays on at a constant intensity (start 6) so the
  keyboard and desk are lit by the screen in the dark room.

## The copy on the screen — one instance, three homes

The hero copy is created ONCE by `HeroFunnel` (it owns the input state and the submit)
and handed to `HeroLamp` as `screen` (a React element). `HeroLamp` decides where it
renders:

| mode | where | who sees it |
|---|---|---|
| server / no JS | flat, in `.ld-funnel-in` inside the section | crawlers, no-JS visitors |
| `fallback` (reduced motion, no WebGL) and **narrow (< 640 px)** | flat, same | mobile, a11y-motion, old GPUs |
| `scene` | inside the laptop screen via drei `<Html transform>` | everyone else |

Rules for the in-screen render:
- drei `Html` must be given a **`portal`** ref to a container `HeroLamp` renders as a
  sibling of the canvas host (`.ld-screen-portal`: `position:absolute; inset:0;
  z-index:1`), because drei's default portal target is the canvas's parent — which is
  `aria-hidden`, `pointer-events:none` and masked. The portal container is NOT
  aria-hidden; the input must be focusable and typeable, and the copy readable by AT.
- `transform` mode, `occlude` off, `zIndexRange={[2, 1]}`, `pointerEvents="auto"`.
  Sizing is calibrated in code: the wrapper's CSS width/`distanceFactor` are chosen so
  the DOM box matches the screen plane's inner bezel (project the screen's corners to
  px and compare; keep the constants together with a comment recording the numbers).
- The copy keeps its existing classes (`.ld-funnel-in`, `.ld-funnel-h1`, `.ld-ask`,
  …) so the typography, the dark-state rules and the page-wide transition apply
  unchanged; a `.ld-funnel-in--screen` modifier tightens spacing for the screen.
- **No flash between homes.** The head script (already sets the theme for `/`) also
  sets `data-lb-3d` on `<html>` when: `pathname === '/'`, WebGL2 is available,
  `innerWidth ≥ 640`, and `prefers-reduced-motion` is not `reduce`. CSS hides the flat
  copy under `html[data-lb-3d]` so it never paints before the scene takes over. If
  `HeroLamp` later lands in `fallback`, it removes the attribute so the flat copy
  shows. No-JS never sets it → the flat copy is visible. Same removal-on-unmount
  discipline as `data-lb-theme`.
- The `ideaText` store field, `setIdeaText`, `useIdeaText`, `ideaTexture.ts` and their
  tests are deleted. `HeroFunnel`'s `onChange` keeps `lightOn()`.

## Brand rules re-checked

One lamp; yellow is light only (the screen is midnight/cream; Start fills yellow only
when lit — its existing rule); Lora on the screen because the copy IS the DOM copy;
no ray fan; the laptop's hex is the owner's exception; both states complete stills;
0 fps at rest (drei `Html` updates its transform only on rendered frames).

## Performance

No new dependencies (drei is installed). The CSS 3D transform is one composited layer.
Mobile unchanged (flat copy, lamp only, no GLB). Typing does not touch the canvas at all
now — the strike tween is the only thing typing triggers.

## Out of scope

A stylised laptop; mobile laptop; the desk's depth.
