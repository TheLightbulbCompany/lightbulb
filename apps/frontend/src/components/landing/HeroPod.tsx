"use client";

import { useEffect, useRef } from "react";
import * as THREE from "three";
import gsap from "gsap";
import { ScrollTrigger } from "gsap/ScrollTrigger";

gsap.registerPlugin(ScrollTrigger);

const PARTICLE_COUNT = 5200;
const POD_RADIUS = 2.15;
// Literal hex, NOT var(--token): THREE.Color parses the string itself and has
// no access to the CSS cascade. MOSS is --forest-soft; keep it in sync with
// globals.css by hand.
const INK = new THREE.Color("#0a3d2a");
const MOSS = new THREE.Color("#4a9e74");

/**
 * The hero "pod" — a breathing sphere of ink-dot particles, the visual
 * metaphor for an isolated container. Dark particles on cream (instead of
 * the usual glow-on-black) keep it in the editorial print language.
 *
 * Pointer movement tilts the pod; scrolling past the hero disperses it.
 * Honors prefers-reduced-motion (static frame), pauses offscreen/hidden,
 * and quietly renders nothing if WebGL is unavailable.
 */
export function HeroPod() {
  const mountRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const mount = mountRef.current;
    if (!mount) return;

    let renderer: THREE.WebGLRenderer;
    try {
      renderer = new THREE.WebGLRenderer({ alpha: true, antialias: true });
    } catch {
      return; // No WebGL — the CSS grid backdrop carries the hero alone.
    }

    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    renderer.setClearColor(0x000000, 0);
    mount.appendChild(renderer.domElement);

    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(35, 1, 0.1, 50);
    camera.position.z = 9;

    const group = new THREE.Group();
    scene.add(group);

    // Fibonacci-distributed shell with slight depth jitter, so the sphere
    // reads as a cloud rather than a perfect lattice.
    const basePositions = new Float32Array(PARTICLE_COUNT * 3);
    const phases = new Float32Array(PARTICLE_COUNT);
    const colors = new Float32Array(PARTICLE_COUNT * 3);
    const golden = Math.PI * (3 - Math.sqrt(5));
    for (let i = 0; i < PARTICLE_COUNT; i++) {
      const y = 1 - (i / (PARTICLE_COUNT - 1)) * 2;
      const r = Math.sqrt(1 - y * y);
      const theta = golden * i;
      const jitter = 0.82 + Math.pow((i * 0.61803) % 1, 2.4) * 0.3;
      basePositions[i * 3] = Math.cos(theta) * r * POD_RADIUS * jitter;
      basePositions[i * 3 + 1] = y * POD_RADIUS * jitter;
      basePositions[i * 3 + 2] = Math.sin(theta) * r * POD_RADIUS * jitter;
      phases[i] = ((i * 0.7548776662) % 1) * Math.PI * 2;
      const c = (i * 0.38196601) % 1 < 0.14 ? MOSS : INK;
      colors[i * 3] = c.r;
      colors[i * 3 + 1] = c.g;
      colors[i * 3 + 2] = c.b;
    }

    const geometry = new THREE.BufferGeometry();
    const livePositions = basePositions.slice();
    geometry.setAttribute("position", new THREE.BufferAttribute(livePositions, 3));
    geometry.setAttribute("color", new THREE.BufferAttribute(colors, 3));

    const material = new THREE.PointsMaterial({
      size: 0.032,
      vertexColors: true,
      transparent: true,
      opacity: 0.85,
      depthWrite: false,
      sizeAttenuation: true,
    });
    group.add(new THREE.Points(geometry, material));

    // A thin wireframe "containment field" around the cloud.
    const shell = new THREE.LineSegments(
      new THREE.EdgesGeometry(new THREE.IcosahedronGeometry(POD_RADIUS * 1.14, 1)),
      new THREE.LineBasicMaterial({ color: INK, transparent: true, opacity: 0.06 })
    );
    group.add(shell);

    const state = {
      disperse: 0,
      pointerX: 0,
      pointerY: 0,
      running: !reduced,
      inView: true,
    };

    const layout = () => {
      const w = mount.clientWidth;
      const h = mount.clientHeight;
      renderer.setSize(w, h);
      camera.aspect = w / h;
      camera.updateProjectionMatrix();
      // Desktop: pod sits right of the headline. Mobile: centered backdrop.
      group.position.x = w > 768 ? Math.min(3.4, (w / h) * 1.18) : 0;
      group.position.y = w > 768 ? 0.1 : 0.4;
    };
    layout();

    const onPointer = (e: PointerEvent) => {
      state.pointerX = (e.clientX / window.innerWidth) * 2 - 1;
      state.pointerY = (e.clientY / window.innerHeight) * 2 - 1;
    };
    window.addEventListener("pointermove", onPointer, { passive: true });

    const t0 = performance.now();
    let raf = 0;
    let baseRotation = 0;

    const renderFrame = () => {
      const t = (performance.now() - t0) / 1000;
      baseRotation += 0.0014;

      const spread = 1 + state.disperse * 2.4;
      const breathe = 1 + Math.sin(t * 0.7) * 0.022;
      for (let i = 0; i < PARTICLE_COUNT; i++) {
        const p = phases[i];
        const wobble = 1 + Math.sin(t * 0.9 + p) * 0.035;
        const s = spread * breathe * wobble;
        livePositions[i * 3] = basePositions[i * 3] * s;
        livePositions[i * 3 + 1] = basePositions[i * 3 + 1] * s;
        livePositions[i * 3 + 2] = basePositions[i * 3 + 2] * s;
      }
      geometry.attributes.position.needsUpdate = true;

      group.rotation.y += (baseRotation + state.pointerX * 0.3 - group.rotation.y) * 0.05;
      group.rotation.x += (-0.18 - state.pointerY * 0.18 - group.rotation.x) * 0.05;
      shell.rotation.y = -group.rotation.y * 0.4;

      const fade = 1 - state.disperse;
      material.opacity = 0.85 * fade;
      (shell.material as THREE.LineBasicMaterial).opacity = 0.1 * fade;

      renderer.render(scene, camera);
    };

    const loop = () => {
      renderFrame();
      if (state.running && state.inView && !document.hidden) {
        raf = requestAnimationFrame(loop);
      } else {
        raf = 0;
      }
    };

    const wake = () => {
      if (!raf && state.running && state.inView && !document.hidden) {
        raf = requestAnimationFrame(loop);
      }
    };

    const io = new IntersectionObserver(([entry]) => {
      state.inView = entry.isIntersecting;
      wake();
    });
    io.observe(mount);

    const onVisibility = () => wake();
    document.addEventListener("visibilitychange", onVisibility);

    const ro = new ResizeObserver(() => {
      layout();
      if (!state.running) renderFrame();
    });
    ro.observe(mount);

    // Scroll out of the hero -> the pod loses containment and disperses.
    // Skipped under reduced motion so the static frame stays truly static.
    const trigger = reduced
      ? null
      : ScrollTrigger.create({
          trigger: mount.closest(".ld-hero") ?? mount,
          start: "top top",
          end: "bottom 25%",
          scrub: true,
          onUpdate: (self) => {
            state.disperse = self.progress;
          },
        });

    if (reduced) {
      renderFrame(); // one static frame
    } else {
      raf = requestAnimationFrame(loop);
    }

    return () => {
      if (raf) cancelAnimationFrame(raf);
      trigger?.kill();
      io.disconnect();
      ro.disconnect();
      document.removeEventListener("visibilitychange", onVisibility);
      window.removeEventListener("pointermove", onPointer);
      geometry.dispose();
      material.dispose();
      shell.geometry.dispose();
      (shell.material as THREE.LineBasicMaterial).dispose();
      renderer.dispose();
      mount.removeChild(renderer.domElement);
    };
  }, []);

  return <div className="ld-hero-canvas" ref={mountRef} aria-hidden="true" />;
}
