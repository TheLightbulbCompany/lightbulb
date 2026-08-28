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
const COLLAR_BOTTOM_Y = GLASS_NECK_Y - NECK_OVERLAP - COLLAR_RINGS[2][1]; // 2.19
const COLLAR_TOP_Y = COLLAR_BOTTOM_Y + COLLAR_H; // 2.33
const CORD_LEN = LAMP_ANCHOR_Y - COLLAR_TOP_Y; // 0.87
const FIL_LEN = 0.55;

/** [radius, thickness, centre y in group space], top → bottom. */
const COLLAR_STACK = COLLAR_RINGS.map(([r, h], i) => {
  const above = COLLAR_RINGS.slice(0, i).reduce((s, x) => s + x[1], 0);
  return [r, h, COLLAR_TOP_Y - above - h / 2 - LAMP_ANCHOR_Y] as const;
});

// Lit-state targets. Tuned by render (Task 9); these are the starting values.
const SPOT_INTENSITY = 28;
const POINT_INTENSITY = 9;
const EMISSIVE_ON = 6;
const EMISSIVE_OFF = 0.12;

const easeStrike = (t: number) => 1 - Math.pow(1 - t, 3); // fast attack, slow settle

export function Lamp({ litRef, pendulum, colors, shadows, shadowMapSize }: LampProps) {
  const group = React.useRef<THREE.Group>(null);
  const spot = React.useRef<THREE.SpotLight>(null);
  const point = React.useRef<THREE.PointLight>(null);
  const spotTarget = React.useRef<THREE.Object3D>(new THREE.Object3D());
  const glowMat = React.useRef<THREE.SpriteMaterial>(null);

  const bulb = React.useMemo(() => new THREE.Color(colors.bulb), [colors.bulb]);
  const brass = React.useMemo(() => new THREE.Color(colors.brass), [colors.brass]);
  const midnight = React.useMemo(() => new THREE.Color(colors.midnight), [colors.midnight]);
  const cream = React.useMemo(() => new THREE.Color(colors.cream), [colors.cream]);

  const glassGeom = React.useMemo(
    () => new THREE.LatheGeometry(ST64_PROFILE.map(([r, y]) => new THREE.Vector2(r, y)), 56),
    [],
  );
  const glowTex = React.useMemo(() => makeGlowTexture(), []);
  // One material for all three blades: three would leave two of them dark,
  // since only one can be animated.
  const filamentMaterial = React.useMemo(
    () =>
      new THREE.MeshStandardMaterial({
        color: brass,
        emissive: brass,
        emissiveIntensity: EMISSIVE_OFF,
        toneMapped: false,
      }),
    [brass],
  );
  // The React Compiler treats a memoised value as frozen, so the per-frame
  // writes go through a ref. The same effect owns the material's lifetime.
  const filMat = React.useRef<THREE.MeshStandardMaterial | null>(null);
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
    t.position.set(0, -(LAMP_ANCHOR_Y + 2.5), -1.6); // down the wall, in group space
    g.add(t);
    s.target = t;
    return () => {
      g.remove(t);
    };
  }, []);

  useFrame(() => {
    const g = group.current;
    if (!g) return;
    g.rotation.z = pendulum.angle;
    const k = easeStrike(litRef.current);
    if (spot.current) spot.current.intensity = SPOT_INTENSITY * k;
    if (point.current) point.current.intensity = POINT_INTENSITY * k;
    const m = filMat.current;
    if (m) {
      m.emissive.copy(brass).lerp(bulb, k);
      m.emissiveIntensity = EMISSIVE_OFF + (EMISSIVE_ON - EMISSIVE_OFF) * k;
    }
    if (glowMat.current) glowMat.current.opacity = k;
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
        castShadow={shadows}
      >
        <meshPhysicalMaterial
          color={cream}
          transmission={1}
          thickness={0.5}
          roughness={0.08}
          ior={1.5}
          envMapIntensity={1}
          side={THREE.DoubleSide}
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
              <cylinderGeometry args={[0.006, 0.026, FIL_LEN, 6]} />
            </mesh>
          );
        })}
        {TIE_HEIGHTS.map((f) => (
          <mesh key={f} position={[0, -FIL_LEN / 2 + FIL_LEN * f, 0]} rotation={[Math.PI / 2, 0, 0]}>
            <torusGeometry args={[0.1, 0.005, 6, 28]} />
            <meshStandardMaterial color={brass} emissive={brass} emissiveIntensity={0.4} toneMapped={false} />
          </mesh>
        ))}
        {/* halo — additive, tinted by --bulb, opacity driven by lit */}
        <sprite scale={[1.7, 1.7, 1]}>
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
          color={bulb}
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
        <pointLight ref={point} color={bulb} intensity={0} decay={2} distance={7} />
      </group>
    </group>
  );
}
