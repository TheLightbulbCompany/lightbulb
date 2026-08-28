"use client";

import * as React from "react";
import * as THREE from "three";
import { Canvas, useFrame, useThree } from "@react-three/fiber";
import { Environment, Lightformer, PerformanceMonitor } from "@react-three/drei";
import { Bloom, EffectComposer } from "@react-three/postprocessing";
import { useLampTheme } from "./lampStore";
import { SCROLL_IMPULSE, createPendulum } from "./pendulum";
import { cssColor } from "./lampGeometry";
import { GLASS_CENTER_Y, LAMP_ANCHOR_Y, Lamp, easeStrike, type LampColors } from "./Lamp";
import { Desk } from "./Desk";
import { Laptop } from "./Laptop";

const STRIKE_MS = 700;
// The room arriving: the laptop rises from below the frame onto the desk,
// once, on first paint. Not the strike — a returning lit visitor sees it too.
const APPEAR_MS = 1500;
const CAMERA_Z = 8;
const CAMERA_FOV = 40;
// Studio reflections on the glass: dim in the dark, full when lit, ramped on
// the strike's clock rather than stepped at the theme flip.
const ENV_DARK = 0.35;
const ENV_LIT = 1;
// ── The lamp follows the cursor ────────────────────────────────────────────
// The anchor springs toward a target derived from the pointer's position over
// the hero, and the lamp hangs from it. K and DAMP are the tuned per-60Hz-frame
// values (`v += d·0.012; v *= 0.86`) rewritten against real time, because a
// demand loop's frame gaps are irregular — an idle burst must land in the same
// place a smooth 60Hz run would.
const SPRING_K = 0.012 * 60; // per second
const SPRING_DAMP = 0.86; // per 60Hz frame
const MAX_DT = 0.05; // s — a backgrounded tab must not teleport the lamp
const VEL_SETTLE = 0.0004; // world units per 60Hz frame
const POS_SETTLE = 0.002; // world units
// Visible half-height at z = 0: CAMERA_Z · tan(fov/2). Half-width is this
// times the canvas aspect.
const HALF_HEIGHT = CAMERA_Z * Math.tan((CAMERA_FOV / 2) * (Math.PI / 180)); // 2.91
// How close to the side the lamp may get, in world units.
const EDGE_MARGIN = 0.6;
// Pointer y is mapped over the hero's top 70%; below that the lamp is already
// as low as it goes.
const POINTER_Y_SPAN = 0.7;
// Floor for the glass centre: its bottom (0.2 − 0.55) then clears the open
// laptop lid's top (world y ≈ −0.5).
const GLASS_FLOOR_Y = 0.2;
const GLASS_DROP = LAMP_ANCHOR_Y - GLASS_CENTER_Y; // 1.5 — anchor to glass centre
// A dragged pendant trails. The impulse is the anchor's own x velocity, so it
// fires only while the lamp is actually moving and dies with it. This REPLACES
// the old column-crossing impulse, which pushed the pendulum whenever the
// pointer passed within 180px of the hero's centre — a stand-in for "the
// pointer is over the lamp" that stopped being true the moment the lamp
// started following the cursor. It also saturated IMPULSE_CLAMP on any brisk
// move: measured, a 12-step sweep across the centre left the demand loop
// running for 19.5s, against 2.0s for the same sweep off the column.
//
// Tuned by render, NOT derived: a fast drag runs the anchor at ~0.24 world
// units per 60Hz frame, and the pendulum's restoring force balances a
// sustained drive F at sin(θ) = F / 0.014 — so this lands a hard sweep near
// 0.03 rad (≈6px of glass travel), a trail rather than a swing. The brief's
// 0.9 saturated the clamp for the whole transit and rang for eight seconds:
// the pendulum's damping is 0.986 per FRAME, so the tail is logarithmic in
// the peak and a big swing is expensive on a demand loop.
const SWING_FROM_DRIFT = 0.002;
// Below this the swing moves the glass under half a pixel, so the loop stops
// asking for frames even though `pendulum.settled()` (floor 0.0004 rad, i.e.
// 0.02°) is still false. Both the angle AND its per-frame change have to be
// under it — at the top of a swing the angle passes through zero at full speed.
const SWING_INVISIBLE = 0.002;
// Don't rewrite the switch's transform for sub-pixel jitter.
const SWITCH_EPSILON = 0.5;

const clamp = (v: number, lo: number, hi: number) => (v < lo ? lo : v > hi ? hi : v);

type Quality = 2 | 1 | 0; // 2 = full, 1 = no bloom, 0 = no bloom + no shadows

function readColors(): LampColors {
  return {
    bulb: cssColor("--bulb", "#F0C34E"),
    brass: cssColor("--brass", "#B68235"),
    midnight: cssColor("--midnight", "#0F1A2B"),
    cream: cssColor("--cream", "#F5EFE2"),
  };
}

/** Wall whose emissive is the page colour — exact, untonemapped — so the
 *  canvas's bottom mask blends into the page. The lights add the pool. */
function Wall({ litRef, colors }: { litRef: React.MutableRefObject<number>; colors: LampColors }) {
  const mat = React.useRef<THREE.MeshStandardMaterial>(null);
  const scene = useThree((s) => s.scene);
  // Allocated once: `lerp` writes into the material's own colour, never into
  // a fresh one, so the frame loop stays allocation-free.
  const midnight = React.useMemo(() => new THREE.Color(colors.midnight), [colors.midnight]);
  const cream = React.useMemo(() => new THREE.Color(colors.cream), [colors.cream]);
  useFrame(() => {
    if (!mat.current) return;
    // `envMapIntensity={0}` alone does NOT opt a material out of a scene
    // environment: three r184 (WebGLRenderer.js, "material.envMap === null &&
    // scene.environment !== null") OVERWRITES the uniform with
    // scene.environmentIntensity for exactly the materials that have no envMap
    // of their own. Measured: the lit wall sat 8/255 above the page colour with
    // the 0 set. Pointing the wall at the scene's own environment makes its
    // envMap non-null, so its own 0 is the value that ships. Assigned here
    // rather than in an effect because drei's <Environment> fills
    // scene.environment asynchronously; it changes once, so this recompiles
    // the program once.
    if (mat.current.envMap !== scene.environment) {
      mat.current.envMap = scene.environment;
      mat.current.needsUpdate = true;
    }
    const k = easeStrike(litRef.current);
    mat.current.emissive.copy(midnight).lerp(cream, k);
    // The albedo follows the theme too. Whatever light still reaches the wall
    // reflects off THIS colour, and a cream albedo in the dark room lifted the
    // midnight rectangle to grey.
    mat.current.color.copy(midnight).lerp(cream, k);
  });
  return (
    <mesh position={[0, 0, -1.6]} receiveShadow>
      <planeGeometry args={[60, 40]} />
      <meshStandardMaterial
        ref={mat}
        color={colors.midnight}
        emissive={colors.midnight}
        emissiveIntensity={1}
        roughness={0.96}
        // The studio env is for the glass. On the wall it laid a sheen over
        // the exact page colour — the lighter rectangle in the lit room. Only
        // honoured because useFrame gives the material its own envMap; see there.
        envMapIntensity={0}
        toneMapped={false}
      />
    </mesh>
  );
}

/** Drives the strike tween + pendulum, and keeps the demand loop alive only
 *  while something moves. Mounted before the lamp and the wall so its write
 *  to `litRef` lands in the same frame they read it. */
function Driver({
  litRef,
  anchorRef,
  appearRef,
  pendulum,
  visible,
  switchRef,
}: {
  litRef: React.MutableRefObject<number>;
  anchorRef: React.MutableRefObject<{ x: number; y: number }>;
  appearRef: React.MutableRefObject<number>;
  pendulum: ReturnType<typeof createPendulum>;
  visible: boolean;
  switchRef?: React.RefObject<HTMLButtonElement | null>;
}) {
  const theme = useLampTheme();
  const invalidate = useThree((s) => s.invalidate);
  const canvas = useThree((s) => s.gl.domElement);
  const target = theme === "light" ? 1 : 0;
  const startRef = React.useRef<{ from: number; at: number } | null>(null);
  // Where the anchor is heading, and how fast it is going. Written by the
  // pointer listeners, integrated in useFrame — never read during render.
  const anchorTarget = React.useRef({ x: 0, y: LAMP_ANCHOR_Y });
  const anchorVel = React.useRef({ x: 0, y: 0 });
  const lastStep = React.useRef(0);
  // Scratch for the switch projection; allocated once, mutated per frame.
  const proj = React.useRef(new THREE.Vector3());
  const lastSwitchPx = React.useRef({ x: NaN, y: NaN });
  // The button, held in a ref of our own. Writing `switchRef.current.style`
  // directly is a mutation THROUGH a prop, which the React Compiler rejects
  // (unlike `litRef.current = …`, which is a ref write it understands).
  const switchEl = React.useRef<HTMLButtonElement | null>(null);
  React.useEffect(() => {
    switchEl.current = switchRef?.current ?? null;
    // The button exists only after commit, which can be a frame later than the
    // first render — ask for one so its transform is written straight away.
    invalidate();
  }, [switchRef, invalidate]);
  // Stamped on the first rendered frame, not at mount: under frameloop
  // "never" (hero off-screen) the tween would otherwise run out unseen.
  const appearStart = React.useRef<number | null>(null);

  React.useEffect(() => {
    // Already there — a returning visitor's seeded `litRef`, or a flip back
    // mid-tween that lands where we started. No tween, so no frames.
    if (litRef.current === target) return;
    startRef.current = { from: litRef.current, at: performance.now() };
    invalidate();
  }, [target, invalidate, litRef]);

  // Scroll nudges the pendulum; the pointer aims the anchor (the swing then
  // follows from the anchor's velocity, in useFrame). Both request frames.
  // Resize only asks for a repaint: fiber's size/dpr subscription calls
  // setPixelRatio + setSize, which clears the drawing buffer and does NOT
  // invalidate, so the hero goes blank until something else asks for a frame.
  // All of them are unsubscribed while the hero is off-screen: `invalidate` is
  // inert under frameloop="never", so impulses taken then would only pile up
  // unswung velocity to unleash on the way back — and an aim taken then would
  // snap the lamp across the frame on the way back in.
  React.useEffect(() => {
    if (!visible) return;
    // Coming back on-screen: nothing in the scene graph changed, so no commit
    // invalidates for us — and a theme flip taken while frameloop was "never"
    // left a tween queued that would otherwise never be drawn.
    invalidate();
    let lastY = window.scrollY;
    // The hero's rect, cached. Reading it per pointermove forced a layout
    // flush on every mouse event; only scrolling and resizing can move it.
    let rect = canvas.getBoundingClientRect();
    const remeasure = () => {
      rect = canvas.getBoundingClientRect();
    };

    // A frame is only worth asking for when the lamp is actually being sent
    // somewhere new — a pointermove that leaves the target where it already
    // was is a mouse twitch, not motion.
    const aim = (x: number, y: number) => {
      const t = anchorTarget.current;
      if (t.x === x && t.y === y) return;
      t.x = x;
      t.y = y;
      invalidate();
    };
    const rest = () => aim(0, LAMP_ANCHOR_Y);

    const onScroll = () => {
      const d = window.scrollY - lastY;
      lastY = window.scrollY;
      remeasure();
      pendulum.impulse(d * SCROLL_IMPULSE);
      invalidate();
    };
    const onMove = (e: PointerEvent) => {
      if (rect.width === 0 || rect.height === 0) return;
      const fx = (e.clientX - rect.left) / rect.width;
      const fy = (e.clientY - rect.top) / rect.height;
      // Off the hero entirely — including a touch drag that wanders out of it.
      if (fx < 0 || fx > 1 || fy < 0 || fy > 1) {
        rest();
        return;
      }
      // x: the pointer's share of the width, mapped across the visible world
      // and held EDGE_MARGIN in from the sides.
      const halfWidth = HALF_HEIGHT * (rect.width / rect.height);
      const x = clamp(fx - 0.5, -0.5, 0.5) * 2 * (halfWidth - EDGE_MARGIN);
      // y: the anchor drops by exactly what the glass should drop, so the
      // glass centre runs GLASS_CENTER_Y → GLASS_FLOOR_Y over the top 70%.
      const y = LAMP_ANCHOR_Y - (clamp(fy, 0, POINTER_Y_SPAN) / POINTER_Y_SPAN) * (GLASS_CENTER_Y - GLASS_FLOOR_Y);
      aim(x, y);
    };
    // A finger that lifts is a pointer that left; a mouse button that lifts is
    // not, and snapping the lamp home on every click would be a twitch.
    const onUp = (e: PointerEvent) => {
      if (e.pointerType === "touch") rest();
    };
    const onLeave = rest;
    const onResize = () => {
      remeasure();
      invalidate();
    };
    window.addEventListener("scroll", onScroll, { passive: true });
    window.addEventListener("pointermove", onMove, { passive: true });
    window.addEventListener("pointerup", onUp, { passive: true });
    window.addEventListener("pointerleave", onLeave, { passive: true });
    // The pointer leaving the viewport fires on the document, not the window.
    document.addEventListener("mouseleave", onLeave, { passive: true });
    window.addEventListener("resize", onResize, { passive: true });
    return () => {
      window.removeEventListener("scroll", onScroll);
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      window.removeEventListener("pointerleave", onLeave);
      document.removeEventListener("mouseleave", onLeave);
      window.removeEventListener("resize", onResize);
    };
  }, [pendulum, invalidate, visible, canvas]);

  useFrame((state) => {
    let busy = false;
    const now = performance.now();
    if (appearStart.current === null) appearStart.current = now;
    if (appearRef.current < 1) {
      appearRef.current = Math.min(1, (now - appearStart.current) / APPEAR_MS);
      busy = true;
    }
    const s = startRef.current;
    if (s) {
      const t = Math.min(1, (now - s.at) / STRIKE_MS);
      litRef.current = s.from + (target - s.from) * t;
      if (t < 1) busy = true;
      else startRef.current = null;
    }
    state.scene.environmentIntensity = ENV_DARK + (ENV_LIT - ENV_DARK) * easeStrike(litRef.current);

    // ── the anchor springs toward the cursor ──
    // `delta` is fiber's, which a demand loop stretches across idle gaps just
    // as badly; performance.now() is the same clock the tweens above use.
    const dt = lastStep.current ? Math.min(MAX_DT, (now - lastStep.current) / 1000) : 1 / 60;
    lastStep.current = now;
    const frames = dt * 60; // how many 60Hz frames' worth of motion this is
    const damp = Math.pow(SPRING_DAMP, frames);
    const a = anchorRef.current;
    const t = anchorTarget.current;
    const v = anchorVel.current;
    v.x = (v.x + (t.x - a.x) * SPRING_K * dt) * damp;
    v.y = (v.y + (t.y - a.y) * SPRING_K * dt) * damp;
    a.x += v.x * frames;
    a.y += v.y * frames;
    if (
      Math.abs(v.x) > VEL_SETTLE || Math.abs(v.y) > VEL_SETTLE ||
      Math.abs(t.x - a.x) > POS_SETTLE || Math.abs(t.y - a.y) > POS_SETTLE
    ) {
      busy = true;
    } else {
      // Land it exactly, so the demand loop can actually stop.
      a.x = t.x;
      a.y = t.y;
      v.x = 0;
      v.y = 0;
    }
    // The lamp trails the hand that drags it. Scaled by `frames` so a 120Hz
    // display drives it the same amount per second. Clamped inside `impulse`.
    pendulum.impulse(-v.x * SWING_FROM_DRIFT * frames);
    const wasAt = pendulum.angle;
    pendulum.step();
    if (
      !pendulum.settled() &&
      (Math.abs(pendulum.angle) > SWING_INVISIBLE || Math.abs(pendulum.angle - wasAt) > SWING_INVISIBLE)
    ) {
      busy = true;
    }

    // ── the switch follows the glass ──
    // Projected from where the glass actually IS, swing included: rotating
    // (0, −GLASS_DROP) about the anchor by the pendulum's angle. Cheaper than
    // reading the mesh's world matrix, and correct mid-swing, where an
    // anchor-only projection would leave the focus ring 40-odd px off the bulb.
    const sw = switchEl.current;
    if (sw) {
      const sin = Math.sin(pendulum.angle);
      const cos = Math.cos(pendulum.angle);
      const p = proj.current.set(a.x + GLASS_DROP * sin, a.y - GLASS_DROP * cos, 0).project(state.camera);
      const px = ((p.x + 1) / 2) * state.size.width;
      const py = ((1 - p.y) / 2) * state.size.height;
      const last = lastSwitchPx.current;
      // Negated so the NaN of the first frame always writes.
      if (!(Math.abs(px - last.x) < SWITCH_EPSILON && Math.abs(py - last.y) < SWITCH_EPSILON)) {
        last.x = px;
        last.y = py;
        sw.style.transform = `translate(${px.toFixed(1)}px, ${py.toFixed(1)}px)`;
      }
    }

    if (busy) invalidate();
  });
  return null;
}

export default function Scene({ switchRef }: { switchRef?: React.RefObject<HTMLButtonElement | null> }) {
  const lit = useLampTheme() === "light";
  // Seeded, not tweened, on the first render: a returning visitor whose stored
  // theme is already "light" should not watch the strike replay on every load.
  const litRef = React.useRef(lit ? 1 : 0);
  // Where the lamp hangs from. Rest is dead centre at the top of the frame.
  const anchorRef = React.useRef({ x: 0, y: LAMP_ANCHOR_Y });
  const appearRef = React.useRef(0);
  const pendulum = React.useMemo(() => createPendulum(), []);
  const [colors] = React.useState(readColors);
  const [quality, setQuality] = React.useState<Quality>(2);
  const [dpr, setDpr] = React.useState<[number, number]>([1, 1.5]);
  const [visible, setVisible] = React.useState(true);
  const host = React.useRef<HTMLDivElement>(null);
  // The monitor's handlers live out here, outside the Canvas, so they cannot
  // call useThree. `onCreated` hands us the same invalidate Driver uses.
  const invalidate = React.useRef<((frames?: number) => void) | null>(null);
  const narrow = typeof window !== "undefined" && window.innerWidth < 640;

  // Pause rendering entirely when the hero is off-screen.
  React.useEffect(() => {
    const el = host.current;
    if (!el) return;
    const io = new IntersectionObserver((entries) => {
      const e = entries.at(-1); // batched — only the last one is current
      if (e) setVisible(e.isIntersecting);
    }, { threshold: 0 });
    io.observe(el);
    return () => io.disconnect();
  }, []);

  return (
    <div ref={host} className="ld-hero-canvas" aria-hidden="true">
      <Canvas
        frameloop={visible ? "demand" : "never"}
        dpr={dpr}
        // "percentage" = PCFShadowMap, which is what `true` (PCFSoft) already
        // falls back to in three r184 — minus its per-frame deprecation warning.
        shadows={quality > 0 ? "percentage" : false}
        gl={{ antialias: false, powerPreference: "high-performance" }}
        camera={{ position: [0, 0.4, CAMERA_Z], fov: CAMERA_FOV, near: 0.1, far: 40 }}
        onCreated={(state) => {
          invalidate.current = state.invalidate;
        }}
      >
        <PerformanceMonitor
          onDecline={(api) => {
            // The demand loop poisons the monitor: drei times raw
            // performance.now() gaps with no frameloop awareness, so the first
            // frame after an idle reads as a couple of fps and a burst that
            // settles inside its 250ms window never samples at all. A GPU
            // genuinely struggling with this scene sits at 20-40.
            if (api.fps < 20) return;
            // One rung per decline: dpr to 1, then bloom off, then shadows off.
            if (dpr[1] > 1) setDpr(([lo]) => [lo, 1]);
            else setQuality((q) => (q > 0 ? ((q - 1) as Quality) : q));
            invalidate.current?.();
          }}
          onIncline={() => {
            setDpr([1, 1.5]);
            invalidate.current?.(); // dpr change alone clears the buffer
          }}
          flipflops={2}
        />
        <Driver
          litRef={litRef}
          anchorRef={anchorRef}
          appearRef={appearRef}
          pendulum={pendulum}
          visible={visible}
          switchRef={switchRef}
        />
        <ambientLight color={colors.cream} intensity={0.012} />
        {/* Studio reflections, built in code — no CDN HDRI. Intensity is
            written per frame by Driver (ENV_DARK → ENV_LIT on the strike). */}
        <Environment resolution={64}>
          <Lightformer form="rect" intensity={1.4} color={colors.cream} scale={[6, 3, 1]} position={[0, 5, 2]} target={[0, 0, 0]} />
          <Lightformer form="rect" intensity={0.5} color={colors.cream} scale={[3, 4, 1]} position={[-5, 1, 2]} target={[0, 0, 0]} />
          <Lightformer form="circle" intensity={0.3} color={colors.bulb} scale={[2, 2, 1]} position={[4, 2, 3]} target={[0, 0, 0]} />
        </Environment>

        <Wall litRef={litRef} colors={colors} />
        {/* The GLB + its keyboard overlay suspend; the room is drawn without
            them until they land, and the lamp above never waits on them. */}
        <React.Suspense fallback={null}>
          <Desk colors={colors} shadows={quality > 0} />
          <Laptop litRef={litRef} appearRef={appearRef} colors={colors} shadows={quality > 0} />
        </React.Suspense>
        <Lamp
          litRef={litRef}
          anchorRef={anchorRef}
          pendulum={pendulum}
          colors={colors}
          shadows={quality > 0}
          shadowMapSize={narrow ? 512 : 1024}
        />

        {quality === 2 && (
          <EffectComposer multisampling={narrow ? 0 : 4}>
            {/* Threshold above the lit wall's peak (~0.9 cream + ~0.35 pool):
                the filament and halo bloom, the pool does not. At 1.0 the
                whole pool bloomed into a haze over the headline. */}
            <Bloom mipmapBlur luminanceThreshold={1.3} intensity={0.7} radius={0.6} />
          </EffectComposer>
        )}
      </Canvas>
    </div>
  );
}
