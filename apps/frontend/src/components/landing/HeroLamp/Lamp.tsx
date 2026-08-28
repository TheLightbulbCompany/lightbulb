"use client";

import * as React from "react";
import * as THREE from "three";
import { useFrame } from "@react-three/fiber";
import type { Pendulum } from "./pendulum";
import {
  BLADES,
  COLLAR_RINGS,
  GLASS_HEIGHT,
  ST64_PROFILE,
  TIE_HEIGHTS,
} from "./lampGeometry";
import { makeGlowTexture } from "./glowTexture";

export interface LampColors {
  bulb: string;
  brass: string;
  midnight: string;
  cream: string;
}
export interface LampProps {
  /**
   * 0..1 strike progress, written by the scene's tween every frame. A ref and
   * not a value: one tween drives the lamp and the wall together, and a value
   * prop would re-render React 60x a second to do it.
   */
  litRef: React.MutableRefObject<number>;
  /** Stepped by the scene; Lamp only reads `.angle`. */
  pendulum: Pendulum;
  colors: LampColors;
  shadows: boolean;
  shadowMapSize: number;
  /** Extra tilt (radians) summed with the swing — the scene leans the lamp
   *  toward the cursor. Same reason it is a ref as `litRef`. */
  leanRef?: React.MutableRefObject<number>;
}

export const LAMP_ANCHOR_Y = 3.2;
export const GLASS_CENTER_Y = 1.7;

// Walking down from the pivot: cord, collar, then the glass hanging below the
// collar (the lathe is flipped, so its apex points down). The profile starts
// at r=0.14 and LatheGeometry does not cap it, so the neck is an open annulus
// and the collar has to plug it — otherwise you see the glass's own back
// faces through the hole. The r=0.15 ring sleeves NECK_OVERLAP over the rim;
// the r=0.13 ring below it drops on inside the neck.
const NECK_OVERLAP = 0.02;
const COLLAR_H = COLLAR_RINGS.reduce((s, r) => s + r[1], 0); // 0.14
const GLASS_NECK_Y = GLASS_CENTER_Y + GLASS_HEIGHT / 2; // 2.25 — lathe y=0
const COLLAR_BOTTOM_Y = GLASS_NECK_Y - NECK_OVERLAP - COLLAR_RINGS.at(-1)![1]; // 2.19
const COLLAR_TOP_Y = COLLAR_BOTTOM_Y + COLLAR_H; // 2.33
const CORD_LEN = LAMP_ANCHOR_Y - COLLAR_TOP_Y; // 0.87
const FIL_LEN = 0.55;
// Blade radii, top (where the three meet) → base. The glass is a lens: with
// ior 1.5 it magnifies the filament ~1.5×, so these are thinner than they
// look right on paper.
const BLADE_TOP_R = 0.004;
const BLADE_BASE_R = 0.018;
const BLADE_SPREAD = Math.sin((Math.max(...BLADES.map((b) => Math.abs(b.tiltDeg))) * Math.PI) / 180);
/** A tie's radius at height fraction `f` (0 = base, 1 = tip): the outer
 *  blades' offset there plus the blade's own radius, so the ring touches. */
const tieRadius = (f: number) =>
  BLADE_SPREAD * FIL_LEN * (1 - f) + BLADE_BASE_R + (BLADE_TOP_R - BLADE_BASE_R) * f;

/** [radius, thickness, centre y in group space], top → bottom. */
const COLLAR_STACK = COLLAR_RINGS.map(([r, h], i) => {
  const above = COLLAR_RINGS.slice(0, i).reduce((s, x) => s + x[1], 0);
  return [r, h, COLLAR_TOP_Y - above - h / 2 - LAMP_ANCHOR_Y] as const;
});

// Tuned by render. The wall is the page colour (~0.9 linear, untonemapped),
// so anything the lights add clips to white past +0.1 — a pool on cream is a
// white-hot centre fading to cream, and the spot only needs to add ~0.35 at
// its centre (d≈2.3 to the wall). 28 put +1.6 on the wall and bloomed the
// whole hero; 9 on the point light alone added +1.0 right behind the lamp.
const SPOT_INTENSITY = 10;
const POINT_INTENSITY = 2;
// Filament colour = brass → --bulb, scaled: a dull ember off, hot on. The
// blades are light-immune (see the material), so this is their whole look.
const FILAMENT_ON = 3.5;
const FILAMENT_OFF = 0.45;
// Corona just outside the glass; at 1.0 it swallowed the glass edge.
const HALO_OPACITY = 0.5;
const HALO_SCALE = 1.1;

export const easeStrike = (t: number) => 1 - Math.pow(1 - t, 3); // fast attack, slow settle

export function Lamp({ litRef, pendulum, colors, shadows, shadowMapSize, leanRef }: LampProps) {
  const group = React.useRef<THREE.Group>(null);
  const spot = React.useRef<THREE.SpotLight>(null);
  const point = React.useRef<THREE.PointLight>(null);
  const spotTarget = React.useRef<THREE.Object3D>(new THREE.Object3D());
  const glowMat = React.useRef<THREE.SpriteMaterial>(null);

  const bulb = React.useMemo(() => new THREE.Color(colors.bulb), [colors.bulb]);
  // What the lights throw. --bulb alone read yellow-green on the wall; pulled
  // 40% toward cream it is the warm pool a tungsten bulb actually casts. The
  // filament and the halo keep the pure --bulb — that is the source itself.
  const poolColor = React.useMemo(
    () => new THREE.Color(colors.bulb).lerp(new THREE.Color(colors.cream), 0.4),
    [colors.bulb, colors.cream],
  );
  const brass = React.useMemo(() => new THREE.Color(colors.brass), [colors.brass]);
  const midnight = React.useMemo(() => new THREE.Color(colors.midnight), [colors.midnight]);
  const cream = React.useMemo(() => new THREE.Color(colors.cream), [colors.cream]);

  const glassGeom = React.useMemo(
    () => new THREE.LatheGeometry(ST64_PROFILE.map(([r, y]) => new THREE.Vector2(r, y)), 56),
    [],
  );
  const glowTex = React.useMemo(() => makeGlowTexture(), []);
  // One material for all three blades: three would leave two of them dark,
  // since only one can be animated. Unlit on purpose — the point light sits
  // millimetres from the blades and a lit material there is white at any
  // intensity; a filament is its own light, so its colour is set directly.
  const filamentMaterial = React.useMemo(
    () => new THREE.MeshBasicMaterial({ color: brass.clone().multiplyScalar(FILAMENT_OFF), toneMapped: false }),
    [brass],
  );
  // The React Compiler treats a memoised value as frozen, so the per-frame
  // writes go through a ref. The same effect owns the material's lifetime.
  //
  // Strict Mode (dev only) double-invokes both dispose effects below and then
  // reuses what they disposed. Three rebuilds all of it lazily — geometry,
  // program, and the halo's CanvasTexture too (a disposed texture re-uploads
  // from its image on next use; verified by render, the halo glows in dev).
  const filMat = React.useRef<THREE.MeshBasicMaterial | null>(null);
  React.useEffect(() => {
    filMat.current = filamentMaterial;
    return () => {
      filMat.current = null;
      filamentMaterial.dispose();
    };
  }, [filamentMaterial]);
  React.useEffect(
    () => () => {
      glassGeom.dispose();
      glowTex.dispose();
    },
    [glassGeom, glowTex],
  );

  // The spot's target must live in the scene graph and swing with the lamp.
  React.useEffect(() => {
    const g = group.current;
    const s = spot.current;
    if (!g || !s) return;
    const t = spotTarget.current;
    // World (0, 0, -1.6): the wall behind the headline, so the pool's centre
    // is on the canvas. Aimed 2.5 lower it sat on the masked bottom edge and
    // only the cone's spill showed.
    t.position.set(0, -LAMP_ANCHOR_Y, -1.6);
    g.add(t);
    s.target = t;
    return () => {
      g.remove(t);
    };
  }, []);

  useFrame(() => {
    const g = group.current;
    if (!g) return;
    g.rotation.z = pendulum.angle + (leanRef?.current ?? 0);
    const k = easeStrike(litRef.current);
    if (spot.current) spot.current.intensity = SPOT_INTENSITY * k;
    if (point.current) point.current.intensity = POINT_INTENSITY * k;
    const m = filMat.current;
    if (m) {
      m.color
        .copy(brass)
        .lerp(bulb, k)
        .multiplyScalar(FILAMENT_OFF + (FILAMENT_ON - FILAMENT_OFF) * k);
    }
    if (glowMat.current) glowMat.current.opacity = HALO_OPACITY * k;
  });

  return (
    <group ref={group} position={[0, LAMP_ANCHOR_Y, 0]}>
      {/* cord — from the pivot down to the collar */}
      <mesh position={[0, -CORD_LEN / 2, 0]} castShadow={shadows}>
        <cylinderGeometry args={[0.012, 0.012, CORD_LEN, 8]} />
        <meshStandardMaterial color={midnight} roughness={0.9} />
      </mesh>

      {/* collar — three stepped rings, the lowest two seated over the neck */}
      {COLLAR_STACK.map(([r, h, y], i) => (
        <mesh key={i} position={[0, y, 0]} castShadow={shadows}>
          <cylinderGeometry args={[r, r, h, 32]} />
          <meshStandardMaterial color={midnight} metalness={0.6} roughness={0.35} />
        </mesh>
      ))}

      {/* glass — ST64 lathe, flipped: the profile runs neck (y=0) → apex
          upward, but a pendant bulb's apex points down. */}
      <mesh
        geometry={glassGeom}
        position={[0, GLASS_NECK_Y - LAMP_ANCHOR_Y, 0]}
        rotation={[Math.PI, 0, 0]}
        // never: the shadow-casting spot sits inside this mesh — the lamp
        // would occlude its own light and the pool on the wall would never land.
        castShadow={false}
      >
        {/* Front faces only. With DoubleSide, three's transmission pass draws
            the glass's own back wall into the buffer the front wall samples,
            and that inside wall reflects the warm environment — the bulb
            rendered as an amber-filled blob. The neck hole is plugged by the
            collar, so nothing needs the inside drawn. */}
        <meshPhysicalMaterial
          color={cream}
          transmission={1}
          // Thin wall. `thickness` is how far the transmission sample is
          // pushed along the refracted ray; at 0.5 (the bulb's radius) the
          // glass was a thick lens that magnified the blades ~3× into fat
          // amber columns and the ties into full-width bands.
          thickness={0.12}
          roughness={0.08}
          ior={1.5}
          envMapIntensity={1}
        />
      </mesh>

      {/* filament — inside the hanging glass, at its centre. Three tapered
          blades converging at the stem (up, toward the neck), tied twice. */}
      <group position={[0, GLASS_NECK_Y - GLASS_HEIGHT * 0.5 - LAMP_ANCHOR_Y, 0]}>
        {BLADES.map((b) => {
          const tilt = (b.tiltDeg * Math.PI) / 180;
          return (
            <mesh
              key={b.tiltDeg}
              rotation={[0, 0, tilt]}
              // shift by the tilted half-length so the narrow tops meet at x=0
              position={[(Math.sin(tilt) * FIL_LEN) / 2, 0, 0]}
              material={filamentMaterial}
            >
              {/* narrow at the top, wide at the base: a tapered blade */}
              <cylinderGeometry args={[BLADE_TOP_R, BLADE_BASE_R, FIL_LEN, 6]} />
            </mesh>
          );
        })}
        {TIE_HEIGHTS.map((f) => (
          <mesh key={f} position={[0, -FIL_LEN / 2 + FIL_LEN * f, 0]} rotation={[Math.PI / 2, 0, 0]}>
            <torusGeometry args={[tieRadius(f), 0.005, 6, 28]} />
            {/* flat brass, tone-mapped: unlit for the same reason as the
                blades, and it stays brass when they go yellow — the mark's
                ties are its dark lines. */}
            <meshBasicMaterial color={brass} />
          </mesh>
        ))}
        {/* halo — additive, tinted by --bulb, opacity driven by lit */}
        <sprite scale={[HALO_SCALE, HALO_SCALE, 1]}>
          <spriteMaterial
            ref={glowMat}
            map={glowTex}
            color={bulb}
            transparent
            opacity={0}
            depthWrite={false}
            blending={THREE.AdditiveBlending}
            toneMapped={false}
          />
        </sprite>
        {/* the lights live where the filament is */}
        <spotLight
          ref={spot}
          color={poolColor}
          intensity={0}
          angle={0.95}
          penumbra={0.7}
          decay={2}
          distance={0}
          castShadow={shadows}
          shadow-mapSize-width={shadowMapSize}
          shadow-mapSize-height={shadowMapSize}
          shadow-bias={-0.0004}
          shadow-camera-near={0.3}
          shadow-camera-far={12}
        />
        <pointLight ref={point} color={poolColor} intensity={0} decay={2} distance={7} />
      </group>
    </group>
  );
}
