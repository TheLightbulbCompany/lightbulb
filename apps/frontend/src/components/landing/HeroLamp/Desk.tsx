"use client";
import * as React from "react";
import * as THREE from "three";
import type { LampColors } from "./Lamp";

const TOP_Y = -1.5,
  W = 8,
  D = 4.1,
  T = 0.12,
  Z_BACK = -1.6;

export function Desk({ colors, shadows }: { colors: LampColors; shadows: boolean }) {
  const midnight = React.useMemo(() => new THREE.Color(colors.midnight), [colors.midnight]);
  const brass = React.useMemo(() => new THREE.Color(colors.brass), [colors.brass]);
  const zc = Z_BACK + D / 2;
  return (
    <group>
      <mesh position={[0, TOP_Y - T / 2, zc]} receiveShadow castShadow={shadows}>
        <boxGeometry args={[W, T, D]} />
        <meshStandardMaterial color={midnight} roughness={0.92} envMapIntensity={0.15} />
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
