"use client";
import * as React from "react";
import * as THREE from "three";
import type { LampColors } from "./Lamp";

const TOP_Y = -1.5,
  // 13, not 8: with D shortened the desk's near end no longer fans past the
  // frame, so at W 8 both ends sat inside it and it read as a floating plank.
  // The widest the frame gets is the back edge, half-width 6.25 at 1440x900
  // and 5.84 at 1280x720 — 13 runs the desk off both sides at either size.
  W = 13,
  // 2.0, not 4.1. The desk's FRONT edge is what decides legibility, not its
  // height: at D 4.1 the top swallowed .ld-funnel-fine and .ld-funnel-swap,
  // and no TOP_Y clears them (the projection puts the back edge below the
  // swap button only at y = -3.13, where the whole desk is inside the
  // canvas's 140px bottom fade). At D 2.0 the front edge projects to 615 of
  // 797 at 1440x900 and 584 of 757 at 1280x720, above both blocks (633 and
  // 593) at the two sizes the rulings name.
  D = 2.0,
  T = 0.12,
  Z_BACK = -1.6;

export function Desk({ colors, shadows }: { colors: LampColors; shadows: boolean }) {
  const brass = React.useMemo(() => new THREE.Color(colors.brass), [colors.brass]);
  // Midnight pulled 20% toward brass — a dark walnut, still from the tokens.
  // Pure midnight is 0.3% / 0.9% / 2.3% linear albedo: proven by a cream
  // probe that the spot's pool and the laptop's shadow land squarely on the
  // top, and at midnight the top returned none of it — a black wedge in the
  // lit room. This returns ~10% in the red, so the pool reads warm.
  const top = React.useMemo(
    () => new THREE.Color(colors.midnight).lerp(new THREE.Color(colors.brass), 0.2),
    [colors.midnight, colors.brass],
  );
  const zc = Z_BACK + D / 2;
  return (
    <group>
      <mesh position={[0, TOP_Y - T / 2, zc]} receiveShadow castShadow={shadows}>
        <boxGeometry args={[W, T, D]} />
        <meshStandardMaterial color={top} roughness={0.92} envMapIntensity={0.15} />
      </mesh>
      {/* §06: brass draws rules — one hairline along the front edge */}
      <mesh position={[0, TOP_Y + 0.006, Z_BACK + D]}>
        <boxGeometry args={[W, 0.012, 0.012]} />
        <meshStandardMaterial color={brass} metalness={0.7} roughness={0.35} />
      </mesh>
    </group>
  );
}
export const DESK_TOP_Y = TOP_Y;
