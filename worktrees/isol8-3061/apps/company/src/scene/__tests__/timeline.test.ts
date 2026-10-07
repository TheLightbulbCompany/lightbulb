import { describe, expect, it } from "vitest";
import { CHAPTERS } from "@/content";
import { activeProduct, buildTimeline, morph, nearestChapter } from "../timeline";

// Seven 100vh sections + a 60vh footer at a 1000px viewport: tops 0..6000, max = 6600.
const VH = 1000;
const tops = CHAPTERS.map((_, i) => i * VH);
const max = 7 * VH + 0.6 * VH - VH;

describe("buildTimeline", () => {
  const { keys, windows } = buildTimeline(tops, max);

  it("holds each chapter's shape at its section top", () => {
    CHAPTERS.forEach((c, i) => {
      const p = tops[i] / max;
      const key = keys.find(([kp, shape]) => Math.abs(kp - p) < 1e-9 && shape === c.shape);
      expect(key, `${c.id} @ ${p}`).toBeDefined();
    });
  });

  it("morphs only in the gap between two sections", () => {
    // The hold key sits 38% into the gap; the next key is the next section top.
    const hold = keys.find(([p, s]) => s === "brain" && p > tops[1] / max)!;
    expect(hold[0]).toBeCloseTo((tops[1] + (tops[2] - tops[1]) * 0.38) / max, 9);
  });

  it("lights the bulb before the strike section arrives", () => {
    expect(windows.lit[0]).toBeGreaterThan(0);
    expect(windows.lit[1]).toBeCloseTo(tops[1] / max, 9);
  });

  it("sinks the bulb before the vision copy lands", () => {
    expect(windows.vision[1]).toBeLessThan(tops[6] / max);
    expect(windows.vision[0]).toBeGreaterThan(tops[5] / max);
  });

  it("maps scroll to the active product and the nearest chapter", () => {
    expect(activeProduct(windows, tops[2] / max)).toBe(0);
    expect(activeProduct(windows, tops[5] / max)).toBe(3);
    expect(activeProduct(windows, 0)).toBe(-1);
    expect(activeProduct(windows, 1)).toBe(-1);
    expect(nearestChapter(windows, 0)).toBe(0);
    expect(nearestChapter(windows, 1)).toBe(6);
    expect(nearestChapter(windows, tops[3] / max + 0.01)).toBe(3);
  });
});

describe("morph", () => {
  const { keys } = buildTimeline(tops, max);
  const shapes = {
    scatter: new Float32Array([0, 0, 0]),
    brain: new Float32Array([1, 1, 1]),
    network: new Float32Array([2, 2, 2]),
    town: new Float32Array([3, 3, 3]),
    growth: new Float32Array([4, 4, 4]),
    gear: new Float32Array([5, 5, 5]),
  };
  const out = new Float32Array(3);

  it("is exactly the chapter's shape at the section top", () => {
    morph(keys, shapes, tops[2] / max, out);
    expect(Array.from(out)).toEqual([2, 2, 2]);
  });

  it("is between shapes mid-gap and clamps at the ends", () => {
    const mid = (tops[1] + (tops[2] - tops[1]) * 0.7) / max;
    morph(keys, shapes, mid, out);
    expect(out[0]).toBeGreaterThan(1);
    expect(out[0]).toBeLessThan(2);
    morph(keys, shapes, 1.5, out);
    expect(Array.from(out)).toEqual([1, 1, 1]);
    morph(keys, shapes, -1, out);
    expect(Array.from(out)).toEqual([0, 0, 0]);
  });
});
