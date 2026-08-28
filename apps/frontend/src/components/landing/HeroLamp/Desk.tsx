"use client";
import * as React from "react";
import * as THREE from "three";
import type { LampColors } from "./Lamp";

const TOP_Y = -1.5,
  // 22, not 13. The room got deeper for the v3 laptop (below), and a deeper
  // desk is a wider one on screen: at the back edge the frame is 9.8 world
  // units half-wide at 1440x900, so at W 13 the desk's own side edges came
  // inside the frame and it read as a plank. 22 runs it off both sides at
  // every depth the camera sees.
  W = 22,
  // 12.6, not 2.0. The laptop is the stage in v3 and its BASE is 7.3 world
  // units deep at SCALE 0.34 — three and a half times the old desk. A desk it
  // overhangs is not a desk, so the room grew around it: the back edge went
  // from -1.6 to ROOM_BACK_Z (clearing the open lid, which leans back to
  // z -3.49), and the front edge sits at 8.7, 0.41 past the base's front lip.
  // It projects to 756 of 797 at 1440x900 and 718 of 757 at 1280x720 — inside
  // the canvas's 140px bottom fade, which is where a desk running out of frame
  // belongs now that the hero's copy is leaving the page for the screen.
  D = 12.6,
  T = 0.12,
  /** Where the room ends. The wall is drawn here too (Scene.tsx) — the desk's
   *  back edge is flush with it, so neither shows an end. */
  Z_BACK = -3.9;

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
export const ROOM_BACK_Z = Z_BACK;
