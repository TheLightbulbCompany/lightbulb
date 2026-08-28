"use client";

import * as React from "react";
import * as THREE from "three";
import { Canvas, useFrame, useThree } from "@react-three/fiber";
import { Environment, Lightformer, PerformanceMonitor } from "@react-three/drei";
import { Bloom, EffectComposer } from "@react-three/postprocessing";
import { useLampTheme } from "./lampStore";
import { SCROLL_IMPULSE, createPendulum } from "./pendulum";
import { cssColor } from "./lampGeometry";
import { Lamp, type LampColors } from "./Lamp";

const STRIKE_MS = 700;
const CAMERA_Z = 8;
const CAMERA_FOV = 40;

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
  // Allocated once: `lerp` writes into the material's own colour, never into
  // a fresh one, so the frame loop stays allocation-free.
  const midnight = React.useMemo(() => new THREE.Color(colors.midnight), [colors.midnight]);
  const cream = React.useMemo(() => new THREE.Color(colors.cream), [colors.cream]);
  useFrame(() => {
    if (!mat.current) return;
    const k = 1 - Math.pow(1 - litRef.current, 3); // same attack curve as the lamp
    mat.current.emissive.copy(midnight).lerp(cream, k);
  });
  return (
    <mesh position={[0, 0, -1.6]} receiveShadow>
      <planeGeometry args={[60, 40]} />
      <meshStandardMaterial
        ref={mat}
        color={colors.cream}
        emissive={colors.midnight}
        emissiveIntensity={1}
        roughness={0.96}
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
  pendulum,
  visible,
}: {
  litRef: React.MutableRefObject<number>;
  pendulum: ReturnType<typeof createPendulum>;
  visible: boolean;
}) {
  const theme = useLampTheme();
  const invalidate = useThree((s) => s.invalidate);
  const target = theme === "light" ? 1 : 0;
  const startRef = React.useRef<{ from: number; at: number } | null>(null);

  React.useEffect(() => {
    // Already there — a returning visitor's seeded `litRef`, or a flip back
    // mid-tween that lands where we started. No tween, so no frames.
    if (litRef.current === target) return;
    startRef.current = { from: litRef.current, at: performance.now() };
    invalidate();
  }, [target, invalidate, litRef]);

  // Scroll + pointer nudge the pendulum; both request frames. Unsubscribed
  // while the hero is off-screen: `invalidate` is inert under
  // frameloop="never", so impulses taken then would only pile up unswung
  // velocity to unleash on the way back.
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
      const cx = window.innerWidth / 2;
      if (Math.abs(e.clientX - cx) < 140 && e.clientY < 260) {
        if (lastX !== null) pendulum.impulse((e.clientX - lastX) * 0.00004);
        invalidate();
      }
      lastX = e.clientX;
    };
    window.addEventListener("scroll", onScroll, { passive: true });
    window.addEventListener("pointermove", onMove, { passive: true });
    return () => {
      window.removeEventListener("scroll", onScroll);
      window.removeEventListener("pointermove", onMove);
    };
  }, [pendulum, invalidate, visible]);

  useFrame(() => {
    let busy = false;
    const s = startRef.current;
    if (s) {
      const t = Math.min(1, (performance.now() - s.at) / STRIKE_MS);
      litRef.current = s.from + (target - s.from) * t;
      if (t < 1) busy = true;
      else startRef.current = null;
    }
    pendulum.step();
    if (!pendulum.settled()) busy = true;
    if (busy) invalidate();
  });
  return null;
}

export default function Scene() {
  const lit = useLampTheme() === "light";
  // Seeded, not tweened, on the first render: a returning visitor whose stored
  // theme is already "light" should not watch the strike replay on every load.
  const litRef = React.useRef(lit ? 1 : 0);
  const pendulum = React.useMemo(() => createPendulum(), []);
  const [colors] = React.useState(readColors);
  const [quality, setQuality] = React.useState<Quality>(2);
  const [dpr, setDpr] = React.useState<[number, number]>([1, 1.5]);
  const [visible, setVisible] = React.useState(true);
  const host = React.useRef<HTMLDivElement>(null);
  const narrow = typeof window !== "undefined" && window.innerWidth < 640;

  // Pause rendering entirely when the hero is off-screen.
  React.useEffect(() => {
    const el = host.current;
    if (!el) return;
    const io = new IntersectionObserver(([e]) => setVisible(e.isIntersecting), { threshold: 0 });
    io.observe(el);
    return () => io.disconnect();
  }, []);

  return (
    <div ref={host} className="ld-hero-canvas" aria-hidden="true">
      <Canvas
        frameloop={visible ? "demand" : "never"}
        dpr={dpr}
        shadows={quality > 0}
        gl={{ antialias: false, powerPreference: "high-performance" }}
        camera={{ position: [0, 0.4, CAMERA_Z], fov: CAMERA_FOV, near: 0.1, far: 40 }}
        onCreated={({ gl }) => {
          gl.toneMapping = THREE.ACESFilmicToneMapping;
        }}
      >
        <PerformanceMonitor
          onDecline={() => {
            setDpr(([lo]) => [lo, 1]);
            setQuality((q) => (q > 0 ? ((q - 1) as Quality) : q));
          }}
          onIncline={() => setDpr([1, 1.5])}
          flipflops={2}
        />
        <Driver litRef={litRef} pendulum={pendulum} visible={visible} />
        <ambientLight color={colors.cream} intensity={0.03} />
        {/* Studio reflections, built in code — no CDN HDRI. Dim in the dark,
            brighter when lit; stepped on theme change, not tweened, because
            reading the tween's ref during render is what the compiler forbids. */}
        <Environment resolution={64} environmentIntensity={lit ? 1 : 0.35}>
          <Lightformer form="rect" intensity={1.4} color={colors.cream} scale={[6, 3, 1]} position={[0, 5, 2]} target={[0, 0, 0]} />
          <Lightformer form="rect" intensity={0.5} color={colors.cream} scale={[3, 4, 1]} position={[-5, 1, 2]} target={[0, 0, 0]} />
          <Lightformer form="circle" intensity={0.3} color={colors.bulb} scale={[2, 2, 1]} position={[4, 2, 3]} target={[0, 0, 0]} />
        </Environment>

        <Wall litRef={litRef} colors={colors} />
        <Lamp
          litRef={litRef}
          pendulum={pendulum}
          colors={colors}
          shadows={quality > 0}
          shadowMapSize={narrow ? 512 : 1024}
        />

        {quality === 2 && (
          <EffectComposer multisampling={narrow ? 0 : 4}>
            <Bloom mipmapBlur luminanceThreshold={1} intensity={1.1} radius={0.6} />
          </EffectComposer>
        )}
      </Canvas>
    </div>
  );
}
