"use client";

import { useEffect, useRef } from "react";

import { CODE } from "./OsInvite";
import { SPARK } from "./sparkStageBrand";

/* Spark's stage bulb, ported from the Mac app's FirstRun/Stage.swift (StageArt.render) cell for cell:
   the same geometry (the app icon's 1024 px units), the 8x8 Bayer tile, the same ramps and the app's
   pixel eyes. One canvas pixel is one cell; the canvas is scaled by a whole number of device pixels per
   cell, so the pixels stay crisp. The box has a fixed CSS size, so drawing never shifts layout. */

type RGB = [number, number, number];
type Eyes = "O" | "-" | "^";

// The event a Download button sends on hover/focus (true) and leave/blur (false).
export const LIGHT_EVENT = "spark:light";

const B8 = [
  0, 32, 8, 40, 2, 34, 10, 42, 48, 16, 56, 24, 50, 18, 58, 26, 12, 44, 4, 36, 14, 46, 6, 38, 60, 28, 52, 20, 62, 30, 54,
  22, 3, 35, 11, 43, 1, 33, 9, 41, 51, 19, 59, 27, 49, 17, 57, 25, 15, 47, 7, 39, 13, 45, 5, 37, 63, 31, 55, 23, 61, 29,
  53, 21,
].map((r) => (r + 0.5) / 64);
const bay = (x: number, y: number) => B8[(y & 7) * 8 + (x & 7)];
const hex = (h: string): RGB => {
  const n = parseInt(h.slice(1), 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
};
const mix = (a: RGB, b: RGB, t: number): RGB => [
  a[0] + (b[0] - a[0]) * t,
  a[1] + (b[1] - a[1]) * t,
  a[2] + (b[2] - a[2]) * t,
];

const MID = hex(SPARK.midnight),
  YEL = hex(SPARK.yellow),
  CRM = hex(SPARK.cream),
  SPK = hex(SPARK.creature),
  DEEP = hex(SPARK.deep),
  WHT = hex(SPARK.white),
  HOT = hex(SPARK.hot),
  PAPER = hex(SPARK.paper),
  INK = hex(SPARK.ink),
  LIGHT = hex(SPARK.light);
const NIGHT = SPARK.night.map(hex);
const C = 3,
  S = 560 / 502,
  dR = 109 * S;

// The app's eyes, traced from its renders ('#' ink, 'o' light, 'r' rim).
const EYE: Record<Eyes, { dx: number[]; dy: number; g: string[] }> = {
  O: {
    dx: [-13, 5],
    dy: -5,
    g: [
      "..rrrr..",
      ".rr##rr.",
      "rr#o##rr",
      "r#ooo##r",
      "r##o###r",
      "r######r",
      "r####o#r",
      "rr####rr",
      ".rr##rr.",
      "..rrrr..",
    ],
  },
  "-": {
    dx: [-13, 5],
    dy: -5,
    g: ["", "", "", "", "rrrrrrrr", "r######r", "rrrrrrrr"],
  },
  "^": {
    dx: [-14, 4],
    dy: -3,
    g: ["..rrrrrr..", ".rr####rr.", "rr######rr", "r###rr###r", "r##rrrr##r", "rrrr..rrrr"],
  },
};

type Geo = ReturnType<typeof geometry>;

function geometry(probe: CanvasRenderingContext2D, cols: number, rows: number) {
  const W = cols * C,
    H = rows * C,
    ox = W / 2;
  // Room for everything: centre the needles' tips to the collar's tip. Less: centre the bulb itself.
  const top = H >= 703 ? (H - 683) / 2 + 123 : (H - 560) / 2 + 10;
  const U = (x: number, y: number): [number, number] => [ox + (x - 511) * S, top + (y - 262) * S];
  const cu = U(511, 384),
    cr = { x: cu[0], y: cu[1] + dR * 0.02, R: dR * 0.6 };
  const poly = (p: Path2D, pts: [number, number][]) => {
    p.moveTo(pts[0][0], pts[0][1]);
    for (let i = 1; i < pts.length; i++) p.lineTo(pts[i][0], pts[i][1]);
    p.closePath();
  };
  const glass = new Path2D(),
    c = U(511, 371);
  glass.arc(c[0], c[1], dR, 0, Math.PI, true);
  [U(402, 492), U(452, 632), U(570, 632), U(620, 492)].forEach((q) => glass.lineTo(q[0], q[1]));
  glass.closePath();
  const base = new Path2D();
  [
    [452, 643, 118, 28],
    [460, 683, 102, 28],
  ].forEach((r) => {
    const o = U(r[0], r[1]);
    base.rect(o[0], o[1], r[2] * S, r[3] * S);
  });
  poly(base, [U(478, 721), U(545, 721), U(528, 764), U(497, 764)]);
  const fil = new Path2D(),
    ax = 511,
    ay = 618;
  [445, 511, 577].forEach((xt) => {
    [
      [436, 532],
      [541, 606],
    ].forEach((b) => {
      const at = (y: number) => {
        const k = (ay - y) / (ay - 325);
        return [ax + (xt - ax) * k, 17 * k];
      };
      const a0 = at(b[0]),
        a1 = at(b[1]);
      poly(fil, [U(a0[0] - a0[1], b[0]), U(a0[0] + a0[1], b[0]), U(a1[0] + a1[1], b[1]), U(a1[0] - a1[1], b[1])]);
    });
  });
  // The creature's outline: the app's seeded harmonic walk (seed 4).
  let st = 4;
  const next = () => {
    st = (Math.imul(st, 1664525) + 1013904223) >>> 0;
    return st / 4294967296;
  };
  const harm = [1, 2, 3, 4, 5, 7].map((k) => {
    const a = (next() - 0.5) * (k === 2 ? 0.16 : 0.08);
    return [k, a, next() * 6.28];
  });
  const blob = new Path2D();
  for (let i = 0; i <= 120; i++) {
    const th = (i / 120) * 2 * Math.PI;
    let rr = cr.R;
    harm.forEach((h) => (rr += cr.R * h[1] * Math.sin(h[0] * th + h[2])));
    const qx = cr.x + rr * Math.cos(th),
      qy = cr.y + (rr * Math.sin(th)) / 1.05;
    if (i === 0) blob.moveTo(qx, qy);
    else blob.lineTo(qx, qy);
  }
  blob.closePath();
  const mask = (p: Path2D) => {
    const m = new Uint8Array(cols * rows);
    for (let y = 0; y < rows; y++)
      for (let x = 0; x < cols; x++) m[y * cols + x] = probe.isPointInPath(p, x * C + C / 2, y * C + C / 2) ? 1 : 0;
    return m;
  };
  const dist = (m: Uint8Array, inside: boolean) => {
    const big = 1e6,
      D = 1.414,
      d = new Float32Array(cols * rows);
    for (let i = 0; i < d.length; i++) d[i] = m[i] === (inside ? 1 : 0) ? big : 0;
    const at = (x: number, y: number) =>
      x < 0 || y < 0 || x >= cols || y >= rows ? (inside ? 0 : big) : d[y * cols + x];
    for (let y = 0; y < rows; y++)
      for (let x = 0; x < cols; x++) {
        const k = y * cols + x;
        if (d[k] > 0)
          d[k] = Math.min(d[k], at(x - 1, y) + 1, at(x, y - 1) + 1, at(x - 1, y - 1) + D, at(x + 1, y - 1) + D);
      }
    for (let y = rows - 1; y >= 0; y--)
      for (let x = cols - 1; x >= 0; x--) {
        const k = y * cols + x;
        if (d[k] > 0)
          d[k] = Math.min(d[k], at(x + 1, y) + 1, at(x, y + 1) + 1, at(x + 1, y + 1) + D, at(x - 1, y + 1) + D);
      }
    return d;
  };
  const g = mask(glass),
    bl = mask(blob);
  return {
    cols,
    rows,
    W,
    U,
    cr,
    glass: g,
    base: mask(base),
    fil: mask(fil),
    blob: bl,
    gIn: dist(g, true),
    gOut: dist(g, false),
    bIn: dist(bl, true),
    bOut: dist(bl, false),
    cache: {} as Record<string, ImageData>,
  };
}

// One frame at a light level (cached per 1/40 step, so animation is a putImageData per change).
function render(G: Geo, lit: number, eyes: Eyes): ImageData {
  const key = Math.round(Math.max(0, Math.min(1, lit)) * 40) + eyes;
  if (G.cache[key]) return G.cache[key];
  lit = Math.round(lit * 40) / 40;
  const { cols, rows, cr } = G,
    N = cols * rows,
    off = lit < 0.05,
    full = lit > 0.97;
  const img = new ImageData(cols, rows),
    px = img.data;
  const put = (x: number, y: number, c: RGB) => {
    if (x < 0 || y < 0 || x >= cols || y >= rows) return;
    const k = (y * cols + x) * 4;
    px[k] = c[0];
    px[k + 1] = c[1];
    px[k + 2] = c[2];
    px[k + 3] = 255;
  };
  const pick = (r: RGB[], t: number, b: number) => {
    t = Math.max(0, Math.min(0.999, t));
    const f = t * (r.length - 1),
      lo = Math.floor(f);
    return f - lo > b ? r[Math.min(lo + 1, r.length - 1)] : r[lo];
  };
  // the night, measured in the app's own 464 x 960 picture around the bulb
  const lift = -0.2 + 0.22 * lit;
  for (let y = 0; y < rows; y++)
    for (let x = 0; x < cols; x++) {
      const u = (x * C - G.W / 2) / 464 + 0.5,
        v = (y * C - (cr.y - 288.5)) / 960;
      let t = 1 - Math.min(1, Math.hypot((u - 0.5) * 1.1, (v - 0.3) * 1.6) * 0.95);
      t += 0.06 * Math.sin(u * 9 + v * 4) + 0.05 * Math.sin(u * 3.3 - v * 7.1) + lift;
      put(x, y, pick(NIGHT, t, bay(x, y)));
    }
  const stroke = 3.01;
  if (!off && lit > 0.7) {
    const aura = 10 * lit,
      glow = mix(MID, YEL, 0.55);
    for (let i = 0; i < N; i++)
      if (!G.glass[i] && G.gOut[i] < aura) {
        const x = i % cols,
          y = (i / cols) | 0;
        if ((lit - 0.4) * 0.5 * Math.pow(1 - G.gOut[i] / aura, 2) > bay(x, y)) put(x, y, glow);
      }
  }
  const fp = G.U(511, 560),
    hotc = mix(YEL, HOT, 0.6),
    dim = mix(MID, YEL, 0.06),
    offc = mix(MID, CRM, 0.18);
  for (let i = 0; i < N; i++)
    if (G.glass[i] && G.gIn[i] > stroke) {
      const x = i % cols,
        y = (i / cols) | 0,
        b = bay(x, y);
      const X = x * C + C / 2,
        Y = y * C + C / 2;
      if (off) {
        if (0.1 * (1 - Math.hypot(X - cr.x, (Y - cr.y) * 0.8) / (dR * 2.1)) > b) put(x, y, offc);
        continue;
      }
      const depth = 22 * (1 - lit) + 1.5,
        edge = Math.min(1, (G.gIn[i] - stroke) / depth);
      const core =
        lit > 0.5 ? Math.max(0, 1 - Math.hypot(X - fp[0], (Y - fp[1]) * 0.7) / (dR * 1.5 * (lit - 0.35))) : 0;
      put(x, y, core > b + 0.15 ? hotc : edge > b ? YEL : dim);
    }
  if (!off) {
    const fc = mix(YEL, CRM, 0.4 + 0.6 * lit);
    for (let i = 0; i < N; i++) if (G.fil[i] && G.glass[i] && G.gIn[i] > stroke) put(i % cols, (i / cols) | 0, fc);
  }
  const body = off ? mix(SPK, MID, 0.35) : SPK;
  const ramp = [mix(DEEP, body, 0.28), mix(DEEP, body, 0.55), mix(DEEP, body, 0.8), body, mix(body, WHT, 0.35)];
  const halo = [mix(DEEP, body, 0.12), mix(DEEP, body, 0.22)],
    haloCells = (cr.R * 0.1) / C;
  for (let i = 0; i < N; i++) {
    const x = i % cols,
      y = (i / cols) | 0,
      b = bay(x, y);
    if (G.blob[i]) {
      let tt = Math.min(1, (G.bIn[i] * C) / (0.8 * cr.R));
      const dx = x * C - cr.x,
        dy = y * C - cr.y;
      tt = tt * 0.75 + 0.25 * Math.max(0, (-dx - dy) / (1.6 * cr.R)) + (full ? 0.08 : 0);
      put(x, y, pick(ramp, tt, b));
    } else if (G.bOut[i] < haloCells) {
      const h = 1 - G.bOut[i] / haloCells;
      if (h * 1.1 > b) put(x, y, h > 0.55 && b < h - 0.4 ? halo[1] : halo[0]);
    }
  }
  const metal = mix(MID, CRM, 0.42 + 0.58 * Math.min(1, lit / 0.3));
  for (let i = 0; i < N; i++) if ((G.glass[i] && G.gIn[i] <= stroke) || G.base[i]) put(i % cols, (i / cols) | 0, metal);
  const o = G.U(511, 371);
  if (full) {
    const nc = mix(MID, YEL, 0.85);
    for (let j = 0; j < 53; j += 2) {
      const a = ((-130 + j * 5 - 90) * Math.PI) / 180,
        r0 = dR * 1.38,
        r1 = dR * (j % 4 === 0 ? 2.02 : 1.74),
        n = Math.ceil((r1 - r0) / (C * 0.5));
      for (let k = 0; k <= n; k++) {
        const r = r0 + ((r1 - r0) * k) / n;
        put(Math.floor((o[0] + r * Math.cos(a)) / C), Math.floor((o[1] + r * Math.sin(a)) / C), nc);
      }
    }
  }
  // the face
  const E = EYE[eyes],
    ex = Math.round(cr.x / C),
    ey = Math.round((cr.y - cr.R * 0.06) / C),
    rim = mix(body, WHT, 0.6);
  E.dx.forEach((ddx) =>
    E.g.forEach((row, ry) => {
      for (let cx = 0; cx < row.length; cx++) {
        const ch = row[cx];
        if (ch !== ".") put(ex + ddx + cx, ey + E.dy + ry, ch === "#" ? INK : ch === "o" ? LIGHT : rim);
      }
    }),
  );
  if (full) {
    const R = (dR * 1.05) / C,
      ocx = o[0] / C,
      ocy = o[1] / C;
    [
      [0.95, -0.8, 3],
      [1.25, -0.2, 1],
      [-1.15, -0.95, 1],
    ].forEach((s) => {
      const sx = Math.round(ocx + R * s[0]),
        sy = Math.round(ocy + R * s[1]);
      for (let q = -s[2]; q <= s[2]; q++) {
        put(sx + q, sy, PAPER);
        put(sx, sy + q, PAPER);
      }
    });
  }
  return (G.cache[key] = img);
}

const FL = [
  [0.06, 0],
  [0.14, 0.6],
  [0.24, 0.2],
  [0.42, 1],
];

/**
 * `level` a fixed light level (the setup and closing bulbs), or "hero": lights up through the setup steps
 * on load, breathes and blinks while idle, and flickers on full with the happy face while a Download
 * button is hovered or focused (the app's Done). Reduced motion: still frames only.
 */
export function SparkStage({ level, className, label }: { level: number | "hero"; className: string; label?: string }) {
  const box = useRef<HTMLDivElement>(null);
  const cv = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const el = box.current,
      canvas = cv.current,
      ctx = canvas?.getContext("2d");
    if (!el || !canvas || !ctx || typeof ResizeObserver === "undefined") return;
    const fixed = typeof level === "number" ? level : null;
    const hero = fixed === null;
    const minRows = hero ? 236 : 190;
    let G: Geo | null = null,
      drawn: string | null = null;
    const layout = () => {
      const dpr = window.devicePixelRatio || 1;
      const k = Math.max(1, Math.floor((el.clientHeight * dpr) / minRows));
      const cols = Math.floor((el.clientWidth * dpr) / k),
        rows = Math.floor((el.clientHeight * dpr) / k);
      if (cols < 1 || rows < 1 || (G && G.cols === cols && G.rows === rows)) return;
      G = geometry(ctx, cols, rows);
      canvas.width = cols;
      canvas.height = rows;
      canvas.style.width = `${(cols * k) / dpr}px`;
      canvas.style.height = `${(rows * k) / dpr}px`;
      drawn = null;
    };
    const draw = (lit: number, eyes: Eyes) => {
      const key = Math.round(lit * 40) + eyes;
      if (!G || drawn === key) return;
      ctx.putImageData(render(G, lit, eyes), 0, 0);
      drawn = key;
    };

    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    // The invite is the app's Sign-in step: the bulb stays dim until you have an account.
    const invite = hero && CODE.test(new URLSearchParams(window.location.search).get("ref") ?? "");
    const IDLE = invite ? 0.3 : 0.84;
    const anim = hero && !reduce;
    let target = anim && !invite ? 0.3 : IDLE,
      from = target,
      since = performance.now(),
      flick = false,
      cheer = false;
    const start = since;
    let queue: [number, number][] =
      anim && !invite
        ? [
            [450, 0.62],
            [1100, IDLE],
          ]
        : [];
    let blinkAt = since + 2600,
      blinking = 0,
      raf = 0;

    const still = () =>
      fixed === null ? draw(cheer ? 1 : IDLE, cheer ? "^" : "O") : draw(fixed, fixed > 0.97 ? "^" : "O");
    const value = (now: number) => {
      const t = Math.max(0, (now - since) / 1000);
      let v = 1;
      if (flick) {
        let a = [0, from];
        for (const f of FL) {
          if (t < f[0]) {
            v = a[1] + ((f[1] - a[1]) * (t - a[0])) / (f[0] - a[0]);
            break;
          }
          a = f;
        }
      } else {
        const w = (2 * Math.PI) / 0.55; // the app's critically damped spring
        v = target + (from - target) * (1 + w * t) * Math.exp(-w * t);
      }
      if (!cheer) v += 0.06 * Math.sin((2 * Math.PI * now) / 1000 / 2.8);
      return Math.min(1, Math.max(0, v));
    };
    const go = (to: number, flicker: boolean) => {
      const now = performance.now();
      from = value(now);
      target = to;
      since = now;
      flick = flicker;
    };
    const frame = (now: number) => {
      while (queue.length && queue[0][0] <= now - start) go(queue.shift()![1], false);
      let eyes: Eyes = cheer && !(flick && now - since < 420) ? "^" : "O";
      if (!cheer) {
        if (now > blinkAt) {
          blinking = now + 130;
          blinkAt = now + 3200 + Math.random() * 3000;
        }
        if (now < blinking) eyes = "-";
      }
      draw(value(now), eyes);
      raf = requestAnimationFrame(frame);
    };
    const onLight = (e: Event) => {
      if (!hero) return;
      cheer = (e as CustomEvent<boolean>).detail;
      if (!anim) return still();
      queue = [];
      go(cheer ? 1 : IDLE, cheer);
    };

    layout();
    if (anim) raf = requestAnimationFrame(frame);
    else still();
    const ro = new ResizeObserver(() => {
      layout();
      if (!anim) still();
    });
    ro.observe(el);
    window.addEventListener(LIGHT_EVENT, onLight);
    return () => {
      cancelAnimationFrame(raf);
      ro.disconnect();
      window.removeEventListener(LIGHT_EVENT, onLight);
    };
  }, [level]);

  return (
    <div ref={box} className={className} {...(label ? { role: "img", "aria-label": label } : { "aria-hidden": true })}>
      <canvas ref={cv} aria-hidden="true" />
    </div>
  );
}
