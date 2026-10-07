import { CHAPTERS, type ShapeName } from "@/content";

export type Key = [p: number, shape: ShapeName];
export type Windows = {
  /** Each chapter's section top, in scroll-progress units. */
  at: number[];
  lit: [number, number];
  driftIn: [number, number];
  driftOut: [number, number];
  vision: [number, number];
  products: [number, number][];
};

export const clamp01 = (v: number) => Math.min(1, Math.max(0, v));
export const seg = (p: number, [a, b]: readonly [number, number]) => clamp01((p - a) / (b - a));
export const ease = (t: number) => t * t * (3 - 2 * t);

/** Fraction of the gap between two sections that the previous shape holds before morphing. */
const HOLD = 0.38;

/**
 * The timeline is read off the page, not hand-tuned: `tops` are the chapters'
 * offsetTop in page order and `max` is the scrollable height, both in px. Each
 * chapter's shape is fully formed while its section fills the viewport and
 * morphs in the gap to the next one. Everything out is in progress units (0..1).
 */
export function buildTimeline(tops: number[], max: number): { keys: Key[]; windows: Windows } {
  const at = tops.map((t) => t / max);
  const gap = (i: number) => at[i + 1] - at[i];
  const keys: Key[] = [];
  CHAPTERS.forEach((c, i) => {
    keys.push([at[i], c.shape]);
    if (i < CHAPTERS.length - 1) keys.push([at[i] + gap(i) * HOLD, c.shape]);
  });
  keys.push([1, CHAPTERS[CHAPTERS.length - 1].shape]);
  const windows: Windows = {
    at,
    lit: [at[0] + gap(0) * 0.3, at[1]],                                   // strike lands with the strike section
    driftIn: [at[1] + gap(1) * 0.3, at[2]],                               // bulb moves right for the chapters
    driftOut: [at[5] + gap(5) * 0.25, at[5] + gap(5) * 0.7],
    vision: [at[5] + gap(5) * 0.3, at[5] + gap(5) * 0.8],                 // sunk before the copy lands
    products: [2, 3, 4, 5].map((i) => [at[i] - gap(i - 1) * 0.5, at[i] + gap(i) * 0.5]),
  };
  return { keys, windows };
}

/** Writes the cloud for progress `p` into `out` — a held shape, or a lerp between two. */
export function morph(keys: Key[], shapes: Record<ShapeName, Float32Array>, p: number, out: Float32Array): void {
  let k = 0;
  while (k < keys.length - 2 && p >= keys[k + 1][0]) k++;
  const [pa, sa] = keys[k], [pb, sb] = keys[k + 1];
  const A = shapes[sa], B = shapes[sb];
  if (sa === sb || p <= pa) { out.set(A); return; }
  if (p >= pb) { out.set(B); return; }
  const t = ease((p - pa) / (pb - pa));
  for (let i = 0; i < out.length; i++) out[i] = A[i] + (B[i] - A[i]) * t;
}

/** Index into PRODUCTS for the chapter under the viewport, or -1. */
export function activeProduct(w: Windows, p: number): number {
  return w.products.findIndex(([a, b]) => p >= a && p < b);
}

export function nearestChapter(w: Windows, p: number): number {
  let best = 0;
  w.at.forEach((a, i) => { if (Math.abs(p - a) < Math.abs(p - w.at[best])) best = i; });
  return best;
}
