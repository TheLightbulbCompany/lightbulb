"use client";

/* eslint-disable react-hooks/immutability -- this component drives a three.js
   scene graph imperatively: the bulb, the mirror and the scene are mutable
   objects by design, written every frame from useFrame (the R3F pattern), and
   the React Compiler is not enabled for this app. */

import * as React from "react";
import * as THREE from "three";
import { Canvas, useFrame, useThree } from "@react-three/fiber";
import { Bloom, EffectComposer } from "@react-three/postprocessing";
import type { BloomEffect } from "postprocessing";
import { RoomEnvironment } from "three/examples/jsm/environments/RoomEnvironment.js";
import { gsap } from "gsap";
import { ScrollTrigger } from "gsap/ScrollTrigger";
import { CHAPTERS, PRODUCTS } from "@/content";
import { makeShapes } from "./shapes";
import { activeProduct, buildTimeline, ease, morph, nearestChapter, seg, type Key, type Windows } from "./timeline";
import { BULB_REST_Y, CLOUD_Y, FLOOR_Y, GOLD, INK, WARM, buildBulb, buildFloor, type Bulb } from "./bulb";

gsap.registerPlugin(ScrollTrigger);

const CAMERA_Z = 8.1;
const FOV = 34;
const ROT_TURNS = 0.8;
const BLOOM_DARK = 0.9;
const BLOOM_LIT = 0.45;
/** After a scroll the demand loop keeps drawing until the scrub tween settles this close. */
const SETTLE_EPS = 1e-4;

type Quality = 2 | 1; // 2 = bloom, 1 = no bloom

/** DOM the driver writes each frame. Rendered by <Chrome/>; looked up once on mount. */
function chromeEls() {
  const q = <T extends Element>(sel: string) => document.querySelector<T>(sel);
  return {
    rail: Array.from(document.querySelectorAll<HTMLElement>("#rail i")),
    scrub: q<HTMLElement>("#scrubDot"),
    lbl: q<HTMLElement>("#lbl"),
    lblName: q<HTMLElement>("#lblName"),
    lblSub: q<HTMLElement>("#lblSub"),
    leaders: q<SVGSVGElement>("#leaders"),
  };
}

type DriverProps = {
  bulb: Bulb;
  mirror: THREE.Group;
  n: number;
  /** prefers-reduced-motion: hold the strike state, bind nothing to scroll. */
  frozen: boolean;
  bloomRef: React.RefObject<BloomEffect | null>;
  onQualityDecline: () => void;
};

function Driver({ bulb, mirror, n, frozen, bloomRef, onQualityDecline }: DriverProps) {
  const invalidate = useThree((s) => s.invalidate);
  const camera = useThree((s) => s.camera);
  const scene = useThree((s) => s.scene);
  const gl = useThree((s) => s.gl);
  const shapes = React.useMemo(() => makeShapes(n), [n]);
  const timeline = React.useRef<{ keys: Key[]; windows: Windows } | null>(null);
  const progress = React.useRef(0);
  const lastApplied = React.useRef(-1);
  const els = React.useRef<ReturnType<typeof chromeEls> | null>(null);
  const light = React.useRef<THREE.PointLight>(null);
  const key = React.useRef<THREE.DirectionalLight>(null);
  const slow = React.useRef(0);
  const tmp = React.useMemo(() => new THREE.Vector3(), []);
  const groundC = React.useMemo(() => new THREE.Color(), []);

  React.useEffect(() => {
    els.current = chromeEls();
    const pmrem = new THREE.PMREMGenerator(gl);
    scene.environment = pmrem.fromScene(new RoomEnvironment(), 0.04).texture;
    scene.environmentIntensity = 0.25;
    scene.background = INK.clone();
    scene.fog = new THREE.Fog(INK.clone(), 9, 16);

    // The timeline is read off the page: every window derives from where the
    // sections actually sit, so it survives copy changes and viewport resizes.
    const layout = () => {
      const max = document.documentElement.scrollHeight - innerHeight;
      const tops = CHAPTERS.map((c) => document.getElementById(c.id)?.offsetTop ?? 0);
      timeline.current = buildTimeline(tops, max);
      if (frozen) progress.current = timeline.current.windows.at[1]; // the strike, held
      // ScrollTrigger caches start/end at creation; a font swap that changes the
      // page height would leave its `bottom bottom` stale against the new tops.
      if (!frozen) ScrollTrigger.refresh();
      invalidate();
    };
    layout();
    document.fonts?.ready.then(layout);
    addEventListener("resize", layout);
    // The environment cubemap settles a few frames after creation; ask for one more.
    const kick = window.setTimeout(invalidate, 300);
    const st = frozen
      ? null
      : ScrollTrigger.create({
          trigger: document.body, start: "top top", end: "bottom bottom", scrub: 0.8,
          onUpdate: (self) => { progress.current = self.progress; invalidate(); },
        });
    return () => {
      removeEventListener("resize", layout);
      window.clearTimeout(kick);
      st?.kill();
      pmrem.dispose();
    };
  }, [scene, gl, invalidate, frozen]);

  useFrame((state, delta) => {
    const tl = timeline.current;
    if (!tl) return;
    const p = progress.current, t = frozen ? 0 : state.clock.elapsedTime, W = tl.windows;
    const lit = ease(seg(p, W.lit));
    const vis = ease(seg(p, W.vision));
    const drift = ease(seg(p, W.driftIn)) * (1 - ease(seg(p, W.driftOut)));

    // The strike warms the room; the type stays cream.
    groundC.copy(INK).lerp(WARM, lit);
    (scene.background as THREE.Color).copy(groundC);
    (scene.fog as THREE.Fog).color.copy(groundC);
    document.documentElement.style.setProperty("--ground", `#${groundC.getHexString()}`);

    // The bulb stands, turns slowly, drifts right to make room for the chapters.
    const g = bulb.group;
    g.rotation.y = p * Math.PI * 2 * ROT_TURNS + Math.sin(t * 0.25) * 0.04;
    g.rotation.z = Math.sin(t * 0.4) * 0.008;
    g.position.set(drift * 1.2, BULB_REST_Y, 0);
    mirror.rotation.copy(g.rotation);
    mirror.position.set(g.position.x, 2 * FLOOR_Y - g.position.y, 0);
    mirror.scale.set(1, -1, 1);

    light.current?.position.set(g.position.x, g.position.y + 0.2, 0);
    if (light.current) light.current.intensity = lit * 3.2;
    if (key.current) key.current.intensity = lit * 0.35;
    bulb.filMat.emissiveIntensity = lit * 1.5;
    scene.environmentIntensity = 0.25 + lit * 0.35;
    if (bloomRef.current) bloomRef.current.intensity = BLOOM_DARK + (BLOOM_LIT - BLOOM_DARK) * lit;
    bulb.cloudMat.opacity = 0.12 + lit * 0.5;
    bulb.cloudMat.size = 0.012 + lit * 0.004;
    bulb.cloud.rotation.y = -g.rotation.y + Math.sin(t * 0.3) * 0.14; // the idea faces you while the glass turns
    morph(tl.keys, shapes, p, bulb.cloudPos);
    (bulb.cloud.geometry.attributes.position as THREE.BufferAttribute).needsUpdate = true;

    // The camera looks above the bulb for the vision chapter, so it sinks under the copy.
    camera.position.set(0, 0.5 + vis * 0.6, CAMERA_Z + vis * 2.2);
    camera.lookAt(g.position.x * 0.7, -0.3 + vis * 3.3, 0);

    const e = els.current;
    if (e) {
      const active = activeProduct(W, p);
      if (active >= 0 && e.lbl && e.leaders) {
        tmp.set(0, CLOUD_Y + 0.1, 0);
        g.localToWorld(tmp);
        tmp.x += 0.62;
        tmp.project(camera);
        const x = ((tmp.x + 1) / 2) * innerWidth, y = ((1 - tmp.y) / 2) * innerHeight;
        const lx = innerWidth * 0.78, ly = innerHeight * 0.36;
        if (e.lblName) e.lblName.textContent = PRODUCTS[active].name;
        if (e.lblSub) e.lblSub.textContent = PRODUCTS[active].sub;
        e.lbl.style.left = `${lx}px`;
        e.lbl.style.top = `${ly}px`;
        e.lbl.style.opacity = String(drift);
        e.leaders.innerHTML = `<g style="opacity:${drift}"><polyline points="${x},${y} ${lx - 70},${ly} ${lx - 12},${ly}"/><circle cx="${x}" cy="${y}" r="2.5"/></g>`;
      } else if (e.lbl && e.leaders) {
        e.lbl.style.opacity = "0";
        e.leaders.innerHTML = "";
      }
      if (e.scrub) e.scrub.style.left = `${p * 100}%`;
      const chapter = nearestChapter(W, p);
      e.rail.forEach((d, i) => d.classList.toggle("on", i === chapter));
    }

    // Demand loop: keep drawing while the scrub is still moving, then stop at 0 fps.
    if (!frozen && Math.abs(p - lastApplied.current) > SETTLE_EPS) {
      lastApplied.current = p;
      invalidate();
    }

    // Quality ladder: a sustained slow frame rate drops a rung (never climbs back).
    if (delta > 1 / 30) {
      if (++slow.current > 30) {
        slow.current = 0;
        onQualityDecline();
      }
    } else {
      slow.current = 0;
    }
  });

  return (
    <>
      <pointLight ref={light} color={GOLD} intensity={0} distance={14} decay={1.6} />
      <directionalLight ref={key} color="#ffffff" intensity={0} position={[-3, 4, 5]} />
    </>
  );
}

/**
 * Sizes the canvas from its host and asks for frames. On a fresh load the Canvas
 * can come up at the browser's default 300x150 — R3F's own measurement never
 * lands — and under a demand loop nothing is drawn until a window resize forces
 * one (the same bug the product's HeroLamp works around). So the host is
 * measured here directly (mount, fonts ready, every host resize) and a frame
 * requested each time. Mounted last inside the Canvas.
 */
function FirstFrames() {
  const invalidate = useThree((s) => s.invalidate);
  const setSize = useThree((s) => s.setSize);
  const gl = useThree((s) => s.gl);
  React.useEffect(() => {
    const host = gl.domElement.parentElement;
    const fit = () => {
      if (host) {
        const r = host.getBoundingClientRect();
        if (r.width > 0 && r.height > 0) setSize(r.width, r.height);
      }
      invalidate();
    };
    fit();
    const ids = [120, 400, 1200].map((ms) => window.setTimeout(fit, ms));
    document.fonts?.ready.then(fit).catch(() => {});
    const ro = host && typeof ResizeObserver !== "undefined" ? new ResizeObserver(fit) : null;
    if (ro && host) ro.observe(host);
    return () => {
      ids.forEach((id) => window.clearTimeout(id));
      ro?.disconnect();
    };
  }, [gl, setSize, invalidate]);
  return null;
}

export default function Scene({ frozen = false }: { frozen?: boolean }) {
  const narrow = typeof window !== "undefined" && window.innerWidth < 640;
  const n = narrow ? 12_000 : 24_000;
  const bulb = React.useMemo(() => buildBulb(n), [n]);
  // Shares geometry + materials with the bulb; flipped through the floor as its reflection.
  const mirror = React.useMemo(() => {
    const m = bulb.group.clone();
    m.traverse((o) => { o.renderOrder = -2; });
    return m;
  }, [bulb]);
  const floor = React.useMemo(() => buildFloor(), []);
  const [quality, setQuality] = React.useState<Quality>(2);
  const [dpr, setDpr] = React.useState<[number, number]>([1, 1.5]);
  const bloomRef = React.useRef<BloomEffect | null>(null);
  const decline = React.useCallback(() => {
    if (dpr[1] > 1) setDpr([1, 1]);
    else setQuality(1);
  }, [dpr]);

  // Workaround, root cause open (shared with the product's HeroLamp): on a
  // fresh load the Canvas's own first measurement never reaches the renderer —
  // the canvas stays at the browser's 300x150 default and R3F mounts none of
  // the scene until it has a size. A window resize is the one path that
  // demonstrably re-measures, so a few are dispatched after mount.
  React.useEffect(() => {
    const kick = () => window.dispatchEvent(new Event("resize"));
    const ids = [50, 300, 1000].map((ms) => window.setTimeout(kick, ms));
    return () => ids.forEach((id) => window.clearTimeout(id));
  }, []);

  return (
    <div id="gl" aria-hidden="true">
      <Canvas
        frameloop="demand"
        dpr={dpr}
        gl={{ antialias: true, powerPreference: "high-performance", toneMapping: THREE.ACESFilmicToneMapping }}
        camera={{ position: [0, 0.5, CAMERA_Z], fov: FOV, near: 0.1, far: 50 }}
      >
        <primitive object={bulb.group} />
        <primitive object={mirror} />
        <primitive object={floor} />
        <Driver bulb={bulb} mirror={mirror} n={n} frozen={frozen} bloomRef={bloomRef} onQualityDecline={decline} />
        {quality === 2 && (
          <EffectComposer multisampling={narrow ? 0 : 4}>
            <Bloom ref={bloomRef} mipmapBlur luminanceThreshold={1.0} intensity={BLOOM_DARK} radius={0.6} />
          </EffectComposer>
        )}
        <FirstFrames />
      </Canvas>
    </div>
  );
}
