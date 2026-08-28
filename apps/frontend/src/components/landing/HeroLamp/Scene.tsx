"use client";

import * as React from "react";
import * as THREE from "three";
import { Canvas, useFrame, useThree } from "@react-three/fiber";
import { Environment, Lightformer, PerformanceMonitor } from "@react-three/drei";
import { Bloom, EffectComposer } from "@react-three/postprocessing";
import { useLampTheme } from "./lampStore";
import { SCROLL_IMPULSE, createPendulum } from "./pendulum";
import { cssColor } from "./lampGeometry";
import { Lamp, easeStrike, type LampColors } from "./Lamp";
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
// Cursor: the lamp leans toward it (±LEAN_MAX radians, ≈±4°, across the
// viewport width) and crossing its column pushes it.
const LEAN_MAX = 0.14;
const LEAN_RATE = 8; // per second — the lean closes ~1/e of the gap every 125ms
const LEAN_SETTLE = 0.0005;
const COLUMN_HALF_W = 180;
const POINTER_IMPULSE = 0.0002;

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
  leanRef,
  appearRef,
  pendulum,
  visible,
}: {
  litRef: React.MutableRefObject<number>;
  leanRef: React.MutableRefObject<number>;
  appearRef: React.MutableRefObject<number>;
  pendulum: ReturnType<typeof createPendulum>;
  visible: boolean;
}) {
  const theme = useLampTheme();
  const invalidate = useThree((s) => s.invalidate);
  const canvas = useThree((s) => s.gl.domElement);
  const target = theme === "light" ? 1 : 0;
  const startRef = React.useRef<{ from: number; at: number } | null>(null);
  const leanTarget = React.useRef(0);
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

  // Scroll + pointer nudge the pendulum; both request frames. Resize only
  // asks for a repaint: fiber's size/dpr subscription calls setPixelRatio +
  // setSize, which clears the drawing buffer and does NOT invalidate, so the
  // hero goes blank until something else asks for a frame.
  // All three are unsubscribed while the hero is off-screen: `invalidate` is
  // inert under frameloop="never", so impulses taken then would only pile up
  // unswung velocity to unleash on the way back.
  React.useEffect(() => {
    if (!visible) return;
    // Coming back on-screen: nothing in the scene graph changed, so no commit
    // invalidates for us — and a theme flip taken while frameloop was "never"
    // left a tween queued that would otherwise never be drawn.
    invalidate();
    let lastY = window.scrollY;
    let lastX: number | null = null;
    const onScroll = () => {
      const d = window.scrollY - lastY;
      lastY = window.scrollY;
      pendulum.impulse(d * SCROLL_IMPULSE);
      invalidate();
    };
    const onMove = (e: PointerEvent) => {
      const w = window.innerWidth;
      const cx = w / 2;
      // Lean follows the cursor anywhere; the lights swing with the group,
      // so the pool on the wall follows it too.
      leanTarget.current = Math.max(-0.5, Math.min(0.5, (e.clientX - cx) / w)) * LEAN_MAX;
      // Push only when crossing the lamp's column over the canvas, by the
      // pointer's x velocity — a slow drag across builds a swing the same way.
      const r = canvas.getBoundingClientRect();
      const over = e.clientY >= r.top && e.clientY <= r.bottom && Math.abs(e.clientX - cx) < COLUMN_HALF_W;
      if (over && lastX !== null) pendulum.impulse((e.clientX - lastX) * POINTER_IMPULSE);
      lastX = e.clientX;
      invalidate();
    };
    const onResize = () => invalidate();
    window.addEventListener("scroll", onScroll, { passive: true });
    window.addEventListener("pointermove", onMove, { passive: true });
    window.addEventListener("resize", onResize, { passive: true });
    return () => {
      window.removeEventListener("scroll", onScroll);
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("resize", onResize);
    };
  }, [pendulum, invalidate, visible, canvas]);

  useFrame((state, delta) => {
    let busy = false;
    if (appearStart.current === null) appearStart.current = performance.now();
    if (appearRef.current < 1) {
      appearRef.current = Math.min(1, (performance.now() - appearStart.current) / APPEAR_MS);
      busy = true;
    }
    const s = startRef.current;
    if (s) {
      const t = Math.min(1, (performance.now() - s.at) / STRIKE_MS);
      litRef.current = s.from + (target - s.from) * t;
      if (t < 1) busy = true;
      else startRef.current = null;
    }
    state.scene.environmentIntensity = ENV_DARK + (ENV_LIT - ENV_DARK) * easeStrike(litRef.current);
    pendulum.step();
    if (!pendulum.settled()) busy = true;
    const d = leanTarget.current - leanRef.current;
    if (Math.abs(d) > LEAN_SETTLE) {
      // Time-based, so the ease is the same at any frame rate (and a long
      // idle gap on the demand loop simply lands it).
      leanRef.current += d * (1 - Math.exp(-LEAN_RATE * delta));
      busy = true;
    } else {
      leanRef.current = leanTarget.current;
    }
    if (busy) invalidate();
  });
  return null;
}

export default function Scene() {
  const lit = useLampTheme() === "light";
  // Seeded, not tweened, on the first render: a returning visitor whose stored
  // theme is already "light" should not watch the strike replay on every load.
  const litRef = React.useRef(lit ? 1 : 0);
  const leanRef = React.useRef(0);
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
        <Driver litRef={litRef} leanRef={leanRef} appearRef={appearRef} pendulum={pendulum} visible={visible} />
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
          leanRef={leanRef}
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
