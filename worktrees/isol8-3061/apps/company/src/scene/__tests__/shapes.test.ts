import { describe, expect, it } from "vitest";
import { makeShapes } from "../shapes";

const N = 2000;
const radius = (a: Float32Array, i: number) => Math.hypot(a[i * 3], a[i * 3 + 1], a[i * 3 + 2]);

function mulberry32(seed: number) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

describe("makeShapes", () => {
  const shapes = makeShapes(N, mulberry32(7));

  it("produces every shape at n*3 floats with no NaN", () => {
    for (const name of ["scatter", "brain", "network", "town", "growth", "gear"] as const) {
      expect(shapes[name].length).toBe(N * 3);
      expect(shapes[name].some(Number.isNaN)).toBe(false);
    }
  });

  it("keeps the interior shapes inside the glass and the scatter outside it", () => {
    // The glass wall is at radius 0.72 (bulb.ts profile); the tallest growth bar's
    // corner reaches ~0.66, so the bound is the wall with a little clearance.
    for (const name of ["brain", "network", "town", "growth", "gear"] as const) {
      for (let i = 0; i < N; i++) expect(radius(shapes[name], i)).toBeLessThanOrEqual(0.7);
    }
    let outside = 0;
    for (let i = 0; i < N; i++) if (radius(shapes.scatter, i) > 0.8) outside++;
    expect(outside).toBeGreaterThan(N * 0.7);
  });

  it("splits the brain down the middle", () => {
    for (let i = 0; i < N; i++) expect(Math.abs(shapes.brain[i * 3])).toBeGreaterThanOrEqual(0.028);
  });

  it("is deterministic for a given rand", () => {
    const again = makeShapes(N, mulberry32(7));
    expect(again.town).toEqual(shapes.town);
  });
});
