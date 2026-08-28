/**
 * The lamp's anatomy as numbers, per Brand Identity Manual §02 so drift is
 * detectable: ST64 glass (straight sides rising to a single elliptical
 * dome), three tapered blades tied twice, a stepped collar. Scene units:
 * the glass is 1.1 tall.
 */
export const GLASS_HEIGHT = 1.1;
export const GLASS_MAX_RADIUS = 0.34;

/** [radius, height], neck → apex. Fed to THREE.LatheGeometry as Vector2s. */
export const ST64_PROFILE: ReadonlyArray<readonly [number, number]> = [
  [0.14, 0.0],
  [0.15, 0.08],
  [0.2, 0.22],
  [0.27, 0.42],  // straight sides…
  [0.32, 0.62],
  [0.34, 0.78],  // …to the shoulder
  [0.3, 0.92],   // one elliptical dome
  [0.2, 1.02],
  [0.09, 1.08],
  [0.0, 1.1],
];

export interface BladeSpec {
  tiltDeg: number;
}
/** Three, not four — and the two ties are what stop it reading as a sunburst. */
export const BLADES: readonly BladeSpec[] = [{ tiltDeg: -12 }, { tiltDeg: 0 }, { tiltDeg: 12 }];
export const TIE_HEIGHTS: readonly [number, number] = [0.38, 0.72];

/** [radius, height] top → bottom. */
export const COLLAR_RINGS: ReadonlyArray<readonly [number, number]> = [
  [0.16, 0.05],
  [0.15, 0.05],
  [0.13, 0.04],
];

export function cssColor(
  name: "--midnight" | "--cream" | "--bulb" | "--brass",
  fallback: string,
): string {
  if (typeof window === "undefined") return fallback;
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return v || fallback;
}
