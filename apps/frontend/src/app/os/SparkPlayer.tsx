"use client";

import { useEffect } from "react";

import { SPARK } from "./sparkStageBrand";

/* Spark acting on its own page. The notch hangs from the site menu and holds Spark's pixel creature; as
   each pinned section scrolls, the notch says what it heard and what it is doing, and the creature rides
   beside a pointer that clicks the section's demo. Section progress is the scroll position; the first
   screen plays on time. Reduced motion shows every end state and no pointer. `?t=<seconds>` freezes the
   page at that moment of a scripted scroll (for screenshots). Everything here is decoration
   (aria-hidden); the page's words are in OsLanding. */

// The notch's lines, scene by scene: [from, label, words, typeFrom, typeTo, happy].
type Line = [number, string, string, number | null, number | null, boolean];
const L = (f: number, l: string, t: string, a: number | null = null, b: number | null = null, h = false): Line => [f, l, t, a, b, h];
const HEARD = "LISTENING",
  DOING = "WORKING",
  SAYING = "SPEAKING";
const SCENES: Record<number, Line[]> = {
  1: [L(0, HEARD, "Text Sam the beach photos.", 0.8, 1.8), L(1.85, DOING, "Text Sam the beach photos."), L(7.4, SAYING, "Sent. Sam has all three.", null, null, true)],
  2: [L(0, HEARD, "Cross out the old way.", 0.54, 0.62), L(0.62, DOING, "Cross out the old way."), L(0.93, SAYING, "Done. One sentence.", null, null, true)],
  3: [L(0, HEARD, "Book the 3 pm room.", 0.1, 0.3), L(0.3, DOING, "Book the 3 pm room."), L(0.74, SAYING, "Booked. 3 pm, room 2.", null, null, true)],
  4: [
    L(0, HEARD, "Make me a lo-fi beat.", 0, 0.07),
    L(0.08, DOING, "Make me a lo-fi beat."),
    L(0.58, HEARD, "Now circle the second card.", 0.58, 0.64),
    L(0.64, DOING, "Now circle the second card."),
    L(0.86, SAYING, "Done. Your beat's playing.", null, null, true),
  ],
  5: [L(0, HEARD, "Set me up.", 0.04, 0.12), L(0.12, DOING, "Set me up."), L(0.86, SAYING, "All set. Say hi any time.", null, null, true)],
  6: [L(0, HEARD, "Where do my files go?", 0.08, 0.2), L(0.2, DOING, "Where do my files go?"), L(0.62, SAYING, "Nowhere. They stay on your Mac.", null, null, true)],
  7: [L(0, HEARD, "Is my voice stored?", 0.06, 0.18), L(0.18, DOING, "Is my voice stored?"), L(0.5, SAYING, "No. We don't store the audio.", null, null, true)],
  8: [L(0, HEARD, "Circle the download.", 0.08, 0.2), L(0.2, DOING, "Circle the download."), L(0.62, SAYING, "That's the one. See you on your Mac.", null, null, true)],
};
const TYPED_NOTE = "From today 🌊";
const SPOKEN = "“Text Sam the beach photos.”";
const LIVE = "Book the 3 pm room.";
const BOOKED = "Room 2 · booked";
const LOOPS = ["Dusty drums", "Warm bass", "Rainy keys"];
const PLAY = "M3 1.5v11l9.5-5.5z",
  PAUSE = "M3 2h3v10H3zM8 2h3v10H8z";

export function SparkPlayer() {
  useEffect(() => start(), []);
  return (
    <div className="sp-fixed" aria-hidden="true">
      <div id="notch">
        <div className="np">
          <div className="lab" />
          <div className="cap" />
        </div>
        <span id="meas" className="cap" />
      </div>
      <div className="ghost ph" id="gPhoto" />
      <div className="ghost chip" id="gChip" />
      <canvas id="crea" width={56} height={52} />
      <div id="cursor">
        <svg width="34" height="40" viewBox="0 0 34 40">
          <path d="M2 2 L2 29 L9.5 22.5 L14.5 34 L19.5 31.8 L14.6 20.6 L24.5 20.2 Z" />
        </svg>
      </div>
    </div>
  );
}

type RGB = [number, number, number];
type P2 = [number, number];
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
const mix = (a: RGB, b: RGB, t: number): RGB => [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t];
const MID = hex(SPARK.midnight),
  YEL = hex(SPARK.yellow),
  SPK = hex(SPARK.creature),
  DEEP = hex(SPARK.deep),
  WHT = hex(SPARK.white),
  INK = hex(SPARK.ink),
  LIGHT = hex(SPARK.light),
  CRM = hex(SPARK.cream);
const NIGHT = SPARK.night.map(hex);
const EYE: Record<string, { dx: number[]; dy: number; g: string[] }> = {
  O: { dx: [-13, 5], dy: -5, g: ["..rrrr..", ".rr##rr.", "rr#o##rr", "r#ooo##r", "r##o###r", "r######r", "r####o#r", "rr####rr", ".rr##rr.", "..rrrr.."] },
  "-": { dx: [-13, 5], dy: -5, g: ["", "", "", "", "rrrrrrrr", "r######r", "rrrrrrrr"] },
  "^": { dx: [-14, 4], dy: -3, g: ["..rrrrrr..", ".rr####rr.", "rr######rr", "r###rr###r", "r##rrrr##r", "rrrr..rrrr"] },
};
const COLS = 56,
  ROWS = 52,
  CR = { x: 28, y: 27, R: 23 };

const clamp = (v: number, a = 0, b = 1) => Math.max(a, Math.min(b, v));
const seg = (t: number, a: number, b: number) => clamp((t - a) / (b - a));
const ease = (t: number) => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2);
const eo = (t: number) => 1 - Math.pow(1 - t, 3);
const lerp = (a: number, b: number, t: number) => a + (b - a) * t;
const mid = (r: DOMRect): P2 => [r.left + r.width / 2, r.top + r.height / 2];
const fr = (x: number) => x - Math.floor(x);
function arc(p0: P2, p1: P2, t: number, bend = 0.22): P2 {
  const mx = (p0[0] + p1[0]) / 2,
    my = (p0[1] + p1[1]) / 2,
    dx = p1[0] - p0[0],
    dy = p1[1] - p0[1],
    cx = mx - dy * bend,
    cy = my + dx * bend,
    u = 1 - t;
  return [u * u * p0[0] + 2 * u * t * cx + t * t * p1[0], u * u * p0[1] + 2 * u * t * cy + t * t * p1[1]];
}
const typeOut = (txt: string, p: number, caret: boolean) => {
  const a = [...txt],
    n = Math.round(a.length * clamp(p));
  return esc(a.slice(0, n).join("")) + (caret && p < 1 ? "<i></i>" : "");
};
const esc = (s: string) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;");
const zone = (y: number, a: number, b: number, f: number) => clamp((y - (a - f)) / f) * clamp((b + f - y) / f);
const NS = "http://www.w3.org/2000/svg";

type Mark = { layout: () => void; set: (p: number) => void; at: (p: number) => P2 };
// A hand-drawn ring ("ring") or strike-through ("line") sized from its host's box; relaid when the host resizes.
function makeMark(host: HTMLElement, padX: number, padY: number, seed: number, kind: "ring" | "line", made: Node[]): Mark {
  const svg = document.createElementNS(NS, "svg");
  svg.classList.add("mark");
  if (kind === "line") svg.classList.add("line");
  const path = document.createElementNS(NS, "path");
  svg.appendChild(path);
  host.appendChild(svg);
  made.push(svg);
  let len = 0,
    hw = -1,
    hh = -1;
  const layout = () => {
    hw = host.offsetWidth;
    hh = host.offsetHeight;
    const w = hw + padX * 2,
      h = hh + padY * 2;
    svg.style.left = -padX + "px";
    svg.style.top = -padY + "px";
    svg.setAttribute("width", String(w));
    svg.setAttribute("height", String(h));
    svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
    let d = "";
    if (kind === "line") {
      for (let i = 0; i <= 24; i++) {
        const s = i / 24,
          x = 4 + s * (w - 8),
          y = h * 0.54 + 2.2 * Math.sin(s * 6 + seed) + (s - 0.5) * -4;
        d += (i ? "L" : "M") + x.toFixed(1) + " " + y.toFixed(1);
      }
    } else {
      const cx = w / 2,
        cy = h / 2,
        rx = w / 2 - 5,
        ry = h / 2 - 5,
        rot = -0.05;
      for (let i = 0; i <= 96; i++) {
        const s = i / 96,
          th = -2.35 + Math.PI * 2 * 1.1 * s,
          k = 0.96 + 0.03 * Math.sin(3 * th + seed) + 0.06 * s;
        const x = rx * k * Math.cos(th),
          y = ry * k * Math.sin(th) + ry * 0.04 * Math.sin(2 * th + seed);
        d += (i ? "L" : "M") + (cx + x * Math.cos(rot) - y * Math.sin(rot)).toFixed(1) + " " + (cy + x * Math.sin(rot) + y * Math.cos(rot)).toFixed(1);
      }
    }
    path.setAttribute("d", d);
    len = path.getTotalLength?.() ?? 0;
    path.style.strokeDasharray = String(len);
  };
  return {
    layout,
    set: (p) => {
      if (host.offsetWidth !== hw || host.offsetHeight !== hh) layout();
      path.style.strokeDashoffset = String(len * (1 - p));
      svg.style.opacity = p > 0 ? "1" : "0";
    },
    at: (p) => {
      const pt = path.getPointAtLength(len * clamp(p)),
        b = svg.getBoundingClientRect();
      return [b.left + pt.x, b.top + pt.y];
    },
  };
}

// The dithered night sky with three rows of pixel hills, painted at one canvas pixel per 3 CSS pixels.
function paintSky(cv: HTMLCanvasElement, glowX: number) {
  const host = cv.parentElement as HTMLElement,
    cell = 3,
    W = Math.ceil(host.clientWidth / cell),
    H = Math.ceil(host.clientHeight / cell);
  const c = cv.getContext("2d");
  if (!c || !W || !H) return;
  cv.width = W;
  cv.height = H;
  const img = c.createImageData(W, H),
    px = img.data;
  const glow = mix(MID, YEL, 0.16),
    glow2 = mix(MID, YEL, 0.3);
  const hills = [
    { y: 0.7, a: 0.035, f: 2.1, p: 0.4, c: [NIGHT[3], mix(DEEP, SPK, 0.18)] },
    { y: 0.78, a: 0.05, f: 1.3, p: 2.2, c: [DEEP, mix(DEEP, SPK, 0.38)] },
    { y: 0.88, a: 0.045, f: 0.9, p: 4.1, c: [NIGHT[1], mix(DEEP, SPK, 0.22)] },
  ];
  for (let y = 0; y < H; y++)
    for (let x = 0; x < W; x++) {
      const u = x / W,
        v = y / H,
        b = bay(x, y);
      const t = 1 - v * 1.05 + 0.05 * Math.sin(u * 7 + v * 3),
        f = t * 3,
        lo = Math.max(0, Math.min(3, Math.floor(f)));
      let col = NIGHT[3 - Math.min(3, Math.max(0, lo + (f - lo > b ? 1 : 0)))];
      const g = Math.max(0, 1 - Math.hypot((u - glowX) * 1.4, (v - 0.72) * 3.2));
      if (g * g * 0.9 > b) col = g > 0.75 && b < g - 0.6 ? glow2 : glow;
      for (const h of hills) {
        const top = h.y - h.a * Math.sin(u * Math.PI * h.f + h.p) - 0.012 * Math.sin(u * 23 + h.p);
        if (v > top) {
          const d = (v - top) * H;
          col = d < 2.2 ? h.c[1] : d < 9 && (1 - d / 9) * 0.7 > b ? h.c[1] : h.c[0];
        }
      }
      const k = (y * W + x) * 4;
      px[k] = col[0];
      px[k + 1] = col[1];
      px[k + 2] = col[2];
      px[k + 3] = 255;
    }
  let s = 7;
  const rnd = () => {
    s = (Math.imul(s, 1664525) + 1013904223) >>> 0;
    return s / 4294967296;
  };
  for (let i = 0; i < 22; i++) {
    const x = (rnd() * W) | 0,
      y = (rnd() * H * 0.55) | 0,
      k = (y * W + x) * 4,
      c2 = rnd() < 0.25 ? YEL : CRM;
    px[k] = c2[0];
    px[k + 1] = c2[1];
    px[k + 2] = c2[2];
  }
  c.putImageData(img, 0, 0);
}

function start(): (() => void) | undefined {
  const $ = (id: string) => document.getElementById(id) as HTMLElement;
  const canvas = $("crea") as HTMLCanvasElement | null;
  const cctx = canvas?.getContext("2d");
  if (!canvas || !cctx || typeof Path2D === "undefined") return; // no canvas (tests, very old browsers): the page stays still
  const crea = canvas;

  // The creature: a dithered blob with the app's pixel eyes, cached per face.
  const blob = new Path2D();
  {
    let st = 4;
    const next = () => {
      st = (Math.imul(st, 1664525) + 1013904223) >>> 0;
      return st / 4294967296;
    };
    const harm = [1, 2, 3, 4, 5, 7].map((k) => [k, (next() - 0.5) * (k === 2 ? 0.16 : 0.08), next() * 6.28]);
    for (let i = 0; i <= 120; i++) {
      const th = (i / 120) * 2 * Math.PI;
      let rr = CR.R;
      harm.forEach((h) => (rr += CR.R * h[1] * Math.sin(h[0] * th + h[2])));
      const qx = CR.x + rr * Math.cos(th),
        qy = CR.y + (rr * Math.sin(th)) / 1.05;
      if (i) blob.lineTo(qx, qy);
      else blob.moveTo(qx, qy);
    }
    blob.closePath();
  }
  const m = new Uint8Array(COLS * ROWS);
  for (let y = 0; y < ROWS; y++) for (let x = 0; x < COLS; x++) m[y * COLS + x] = cctx.isPointInPath(blob, x + 0.5, y + 0.5) ? 1 : 0;
  const dist = (inside: boolean) => {
    const big = 1e6,
      D = 1.414,
      d = new Float32Array(COLS * ROWS);
    for (let i = 0; i < d.length; i++) d[i] = m[i] === (inside ? 1 : 0) ? big : 0;
    const at = (x: number, y: number) => (x < 0 || y < 0 || x >= COLS || y >= ROWS ? (inside ? 0 : big) : d[y * COLS + x]);
    for (let y = 0; y < ROWS; y++)
      for (let x = 0; x < COLS; x++) {
        const k = y * COLS + x;
        if (d[k] > 0) d[k] = Math.min(d[k], at(x - 1, y) + 1, at(x, y - 1) + 1, at(x - 1, y - 1) + D, at(x + 1, y - 1) + D);
      }
    for (let y = ROWS - 1; y >= 0; y--)
      for (let x = COLS - 1; x >= 0; x--) {
        const k = y * COLS + x;
        if (d[k] > 0) d[k] = Math.min(d[k], at(x + 1, y) + 1, at(x, y + 1) + 1, at(x + 1, y + 1) + D, at(x - 1, y + 1) + D);
      }
    return d;
  };
  const bIn = dist(true),
    bOut = dist(false),
    cache: Record<string, ImageData> = {};
  function creature(eyes: string, ox: number, oy: number, full: boolean) {
    const key = eyes + ox + "," + oy + full;
    if (cache[key]) return cache[key];
    const img = new ImageData(COLS, ROWS),
      px = img.data;
    const put = (x: number, y: number, c: RGB) => {
      if (x < 0 || y < 0 || x >= COLS || y >= ROWS) return;
      const k = (y * COLS + x) * 4;
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
    const ramp = [mix(DEEP, SPK, 0.28), mix(DEEP, SPK, 0.55), mix(DEEP, SPK, 0.8), SPK, mix(SPK, WHT, 0.35)];
    const halo = [mix(DEEP, SPK, 0.12), mix(DEEP, SPK, 0.22)],
      hc = CR.R * 0.1;
    for (let i = 0; i < COLS * ROWS; i++) {
      const x = i % COLS,
        y = (i / COLS) | 0,
        b = bay(x, y);
      if (m[i]) {
        let tt = Math.min(1, bIn[i] / (0.8 * CR.R));
        tt = tt * 0.75 + 0.25 * Math.max(0, (-(x - CR.x) - (y - CR.y)) / (1.6 * CR.R)) + (full ? 0.08 : 0);
        put(x, y, pick(ramp, tt, b));
      } else if (bOut[i] < hc) {
        const h = 1 - bOut[i] / hc;
        if (h * 1.1 > b) put(x, y, h > 0.55 && b < h - 0.4 ? halo[1] : halo[0]);
      }
    }
    const E = EYE[eyes],
      ex = Math.round(CR.x) + ox,
      ey = Math.round(CR.y - CR.R * 0.06) + oy,
      rim = mix(SPK, WHT, 0.6);
    E.dx.forEach((ddx) =>
      E.g.forEach((row, ry) => {
        for (let cx = 0; cx < row.length; cx++) {
          const ch = row[cx];
          if (ch !== ".") put(ex + ddx + cx, ey + E.dy + ry, ch === "#" ? INK : ch === "o" ? LIGHT : rim);
        }
      }),
    );
    return (cache[key] = img);
  }
  let lastFace = "";
  const face = (eyes: string, ox: number, oy: number, full: boolean) => {
    const k = eyes + ox + oy + full;
    if (k === lastFace) return;
    lastFace = k;
    cctx.clearRect(0, 0, COLS, ROWS);
    cctx.putImageData(creature(eyes, ox, oy, full), 0, 0);
  };

  const made: Node[] = []; // nodes added here, removed on unmount
  const add = <K extends keyof HTMLElementTagNameMap>(parent: HTMLElement, tag: K, n: number) =>
    Array.from({ length: n }, () => {
      const e = document.createElement(tag);
      parent.appendChild(e);
      made.push(e);
      return e;
    });

  const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const q = new URLSearchParams(location.search),
    SEEK = q.has("t");
  const notch = $("notch"),
    np = notch.querySelector(".np") as HTMLElement,
    lab = notch.querySelector(".lab") as HTMLElement,
    cap = notch.querySelector(".cap") as HTMLElement,
    meas = $("meas");
  const cursor = $("cursor"),
    gPhoto = $("gPhoto"),
    gChip = $("gChip");
  const S = [1, 2, 3, 4, 5, 6, 7, 8].map((i) => $("s" + i));
  const sec = (k: number) => S[k - 1];
  // 1
  const tiles = [...$("strip").children] as HTMLElement[],
    slots = [...$("pics").children] as HTMLElement[],
    PICK = [0, 2, 3];
  const inp = $("inp"),
    send = $("send"),
    outtxt = $("outtxt"),
    deliv = $("deliv");
  // 2
  const stepEls = [...$("steps").children] as HTMLElement[],
    hclock = $("hclock"),
    sclock = $("sclock"),
    spoken = $("spoken"),
    doneEl = $("done");
  // 3
  const fnkey = $("fnkey"),
    wave = $("wave"),
    live = $("live"),
    slot3 = $("slot3");
  const waveBars = add(wave, "b", 14);
  // 4
  const chips = [...document.querySelectorAll<HTMLElement>(".sp .dw-bin .chip:not(.off)")],
    regs = [...document.querySelectorAll<HTMLElement>(".sp .reg")],
    lanes = [...document.querySelectorAll<HTMLElement>(".sp .lane")];
  const phead = $("phead"),
    play = $("play"),
    playIco = $("playIco"),
    posLcd = $("pos"),
    grid = $("grid");
  const meters = [...document.querySelectorAll<HTMLElement>(".sp .meter")].map((mt) => add(mt, "b", 7));
  meters.forEach((bars) => bars.forEach((bb, j) => (bb.style.height = 4 + j * 3.4 + "px")));
  const specBars = add($("spec"), "b", 16);
  const MCOL = [SPARK.yellow, SPARK.creature, SPARK.paper];
  const NOTES: number[][][] = [[], [], []];
  for (let s = 0; s < 32; s++) {
    if (s % 2 === 0) NOTES[0].push([s / 32 + 0.004, 0.012, s % 4 === 0 ? 0.55 : 0.2, 0.3]);
    if (s % 8 === 3) NOTES[0].push([s / 32 + 0.004, 0.012, 0.15, 0.12]);
  }
  [[0, 3, 0.55], [4, 2, 0.35], [6, 2, 0.45], [8, 3, 0.6], [12, 4, 0.4], [16, 3, 0.55], [20, 2, 0.35], [22, 2, 0.45], [24, 3, 0.6], [28, 4, 0.3]].forEach(([s, l, y]) =>
    NOTES[1].push([s / 32 + 0.004, l / 32 - 0.01, y, 0.16]),
  );
  [[0.62, 0.48, 0.3], [0.55, 0.4, 0.22], [0.68, 0.5, 0.36], [0.58, 0.44, 0.26]].forEach((ch, k) =>
    ch.forEach((yy, j) => {
      NOTES[2].push([k / 4 + 0.006, j ? 3 / 32 : 7 / 32 - 0.014, yy, 0.1]);
      if (j) NOTES[2].push([k / 4 + 4 / 32, 3 / 32 - 0.01, yy - 0.06 * j, 0.1]);
    }),
  );
  const notes: [HTMLElement, number][] = [];
  regs.forEach((r, k) => {
    const e = document.createElement("em");
    e.textContent = LOOPS[k];
    r.appendChild(e);
    made.push(e);
    NOTES[k].forEach(([x, w, y, h]) => {
      const i = document.createElement("i");
      Object.assign(i.style, { left: x * 100 + "%", width: w * 100 + "%", top: y * 100 + "%", height: h * 100 + "%" });
      r.appendChild(i);
      made.push(i);
      notes.push([i, x]);
    });
  });
  // 5
  const sus = [...document.querySelectorAll<HTMLElement>(".sp .su")],
    targets = [0, 1, 2, 3].map((i) => $("t" + i)),
    hi = $("hi");
  // 6
  const pl = [$("pl0"), $("pl1")];
  // 7
  const q5 = $("q5") as HTMLDetailsElement;
  let q5auto = false;
  const q5click = () => (q5auto = true);
  q5.querySelector("summary")?.addEventListener("click", q5click);
  // hovering a Download lights the creature up
  let lit = false;
  const onLight = (e: Event) => (lit = !!(e as CustomEvent).detail);
  window.addEventListener("spark:light", onLight);

  let phone = false,
    navB = 70;
  const strikes = stepEls.map((li, i) => makeMark(li.querySelector(".st") as HTMLElement, 8, 8, i * 1.7, "line", made));
  const ringCard = makeMark($("circCard"), 20, 14, 1.1, "ring", made);
  const ring6 = makeMark($("stays"), 14, 12, 2.2, "ring", made);
  const ring8 = makeMark($("r8"), 26, 20, 1.3, "ring", made);
  function layout() {
    phone = innerWidth <= 640;
    navB = document.querySelector(".sp .landing-nav")?.getBoundingClientRect().bottom ?? 70;
    notch.style.top = navB + "px";
    [...strikes, ringCard, ring6, ring8].forEach((mk) => mk.layout());
    paintSky($("sky1") as HTMLCanvasElement, 0.42);
    paintSky($("sky8") as HTMLCanvasElement, 0.72);
  }

  function render(T: number, now: number) {
    const vw = innerWidth,
      vh = innerHeight,
      y = scrollY,
      home: P2 = [vw / 2, navB + (phone ? 40 : 52)];
    if (reduce) T = 99;
    const P = [0],
      Z = [clamp(1 - y / (vh * 0.3))];
    for (let k = 2; k <= 8; k++) {
      const s = sec(k),
        r = s.offsetHeight - vh;
      P[k] = reduce ? 1 : clamp((y - s.offsetTop) / r);
      Z[k] = zone(y, s.offsetTop, s.offsetTop + r, vh * 0.2);
    }
    Z[1] = Z[0];
    let own = 1;
    for (let k = 2; k <= 8; k++) if (Z[k] > Z[own]) own = k;
    const st = { want: 0, label: "", full: "", typedP: 1, cur: null as P2 | null, curOp: 1, ro: 0, look: null as P2 | null, happy: false };
    type Ghost = { cls: string; txt?: string; x: number; y: number; w: number; h: number };
    const carry: { p: Ghost | null; c: Ghost | null } = { p: null, c: null }; // the photo or loop being dragged
    const says = (k: number) => {
      if (own !== k) return;
      const p = k === 1 ? T : P[k];
      for (const [f, l, t, a, b, h] of SCENES[k])
        if (p >= f) {
          st.label = l;
          st.full = t;
          st.typedP = a != null && b != null ? seg(p, a, b) : 1;
          st.happy = h;
        }
    };

    /* 1 · text Sam the photos */
    {
      const A0 = 1.95,
        D = 0.95,
        act = own === 1;
      let prev = home;
      tiles.forEach((t) => t.classList.remove("picked"));
      PICK.forEach((ti, i) => {
        const a = A0 + i * D,
          u = seg(T, a, a + D),
          tile = tiles[ti],
          slot = slots[i],
          tr = tile.getBoundingClientRect(),
          sr = slot.getBoundingClientRect();
        if (u >= 0.45) tile.classList.add("picked");
        slot.style.opacity = u >= 0.92 ? (T >= 6.65 ? "1" : ".6") : "0";
        if (!act || u <= 0) {
          prev = i ? mid(slots[i - 1].getBoundingClientRect()) : home;
          return;
        }
        if (u < 0.42) st.cur = arc(prev, [tr.left + tr.width * 0.45, tr.top + tr.height * 0.4], ease(seg(u, 0, 0.42)), 0.18);
        else if (u < 0.92) {
          const g = ease(seg(u, 0.5, 0.9)),
            Lx = lerp(tr.left, sr.left, g),
            Tp = lerp(tr.top, sr.top, g),
            W = lerp(tr.width, sr.width, g),
            H = lerp(tr.height, sr.height, g),
            lift = seg(u, 0.42, 0.5);
          carry.p = { cls: "ghost ph p" + (ti + 1), x: Lx, y: Tp - 10 * lift, w: W, h: H };
          st.cur = [Lx + W * 0.45, Tp - 10 * lift + H * 0.4];
        } else st.cur = [sr.left + sr.width * 0.45, sr.top + sr.height * 0.4];
        st.look = st.cur;
        prev = mid(sr);
      });
      const ir = inp.getBoundingClientRect(),
        sr = send.getBoundingClientRect(),
        last = slots[2].getBoundingClientRect();
      const typed = seg(T, 5.15, 6.2),
        sent = T >= 6.65;
      inp.innerHTML = sent ? "" : typed > 0 ? typeOut(TYPED_NOTE, typed, true) : "";
      send.style.transform = `scale(${1 - 0.12 * Math.sin(Math.PI * seg(T, 6.5, 6.65))})`;
      const ap = eo(seg(T, 6.65, 6.9));
      outtxt.style.opacity = String(ap);
      outtxt.style.transform = `translateY(${(1 - ap) * 14}px)`;
      deliv.style.opacity = String(seg(T, 6.95, 7.1));
      if (act) {
        st.want = seg(T, 0.4, 0.8);
        st.ro = Math.min(seg(T, 1.9, 2.3), 1 - seg(T, 7.3, 7.7));
        says(1);
        if (T >= 7.4) st.happy = T < 9;
        if (T >= 4.8 && T < 6.5) {
          st.cur =
            T < 5.15
              ? arc(mid(last), [ir.left + 40, ir.top + ir.height / 2], ease(seg(T, 4.8, 5.15)), -0.15)
              : [ir.left + 40 + Math.min(150, typed * 150), ir.top + ir.height / 2 + 4];
          st.look = st.cur;
        }
        if (T >= 6.2) {
          st.cur =
            T < 6.5
              ? arc([ir.left + 190, ir.top + ir.height / 2], mid(sr), ease(seg(T, 6.2, 6.5)), 0.2)
              : [sr.left + sr.width / 2 - 2 + 2 * seg(T, 6.5, 6.6), sr.top + sr.height / 2];
          st.look = st.cur;
        }
        if (T >= 7.6) st.curOp = 1 - seg(T, 7.6, 8.0);
        if (T < 1.9) st.cur = null;
      }
    }

    /* 2 · one sentence vs the clicking */
    {
      const p = P[2];
      stepEls.forEach((li, i) => li.classList.toggle("on", p >= 0.06 + i * 0.07));
      const hs = Math.round(lerp(0, 72, seg(p, 0.04, 0.46)));
      hclock.textContent = `${Math.floor(hs / 60)}:${String(hs % 60).padStart(2, "0")}`;
      spoken.innerHTML = typeOut(SPOKEN, seg(p, 0.08, 0.2), true);
      sclock.textContent = `0:0${Math.round(lerp(0, 4, seg(p, 0.08, 0.24)))}`;
      const dn = eo(seg(p, 0.24, 0.3));
      doneEl.style.opacity = String(dn);
      doneEl.style.transform = `scale(${0.85 + 0.15 * dn})`;
      const S0 = 0.64,
        SW = 0.045,
        k = Math.floor((p - S0) / SW),
        u = (p - S0) / SW - k;
      strikes.forEach((mk, i) => {
        const sp = i < k ? 1 : i === k ? seg(u, 0.18, 1) : 0;
        mk.set(sp);
        stepEls[i].classList.toggle("x", sp >= 1);
      });
      if (own === 2) {
        st.want = seg(p, 0.5, 0.55);
        st.ro = Math.min(seg(p, 0.6, 0.64), 1 - seg(p, 0.92, 0.97));
        says(2);
        if (p >= 0.6 && p < 0.64) st.cur = arc(home, strikes[0].at(0), ease(seg(p, 0.6, 0.64)), 0.2);
        else if (k >= 0 && k < 6)
          st.cur = u < 0.18 ? (k ? arc(strikes[k - 1].at(1), strikes[k].at(0), ease(seg(u, 0, 0.18)), 0.1) : strikes[0].at(0)) : strikes[k].at(seg(u, 0.18, 1));
        else if (k >= 6) {
          const e = strikes[5].at(1),
            o = eo(seg(p, 0.91, 0.96));
          st.cur = [e[0] + 30 * o, e[1] + 40 * o];
          st.curOp = 1 - seg(p, 0.93, 0.98);
        }
        if (st.cur) st.look = st.cur;
      }
    }

    /* 3 · how it works */
    {
      const p = P[3],
        held = p >= 0.06 && p < 0.66;
      fnkey.classList.toggle("down", held);
      const tp = seg(p, 0.1, 0.3);
      live.innerHTML = tp > 0 ? typeOut(LIVE, tp, true) : "&nbsp;";
      waveBars.forEach(
        (bb, j) => (bb.style.transform = `scaleY(${held && p < 0.32 ? 1 + 5 * Math.abs(Math.sin(p * 80 + j * 0.9)) * Math.abs(Math.sin(j * 1.3 + p * 23)) : 1})`),
      );
      const booked = p >= 0.54;
      slot3.className = "ev " + (booked ? "booked" : "free");
      slot3.textContent = booked ? BOOKED : "Free";
      slot3.style.transform = `scale(${1 - 0.05 * Math.sin(Math.PI * seg(p, 0.5, 0.54))})`;
      if (own === 3) {
        st.want = seg(p, 0.06, 0.1);
        st.ro = Math.min(seg(p, 0.32, 0.36), 1 - seg(p, 0.74, 0.8));
        says(3);
        const r = slot3.getBoundingClientRect(),
          tgt: P2 = [r.left + r.width * 0.3, r.top + r.height * 0.55];
        if (p >= 0.34) {
          const o = eo(seg(p, 0.6, 0.7));
          st.cur = p < 0.5 ? arc(home, tgt, ease(seg(p, 0.34, 0.5)), 0.2) : [tgt[0] + 26 * o, tgt[1] + 30 * o];
          st.look = tgt;
          st.curOp = 1 - seg(p, 0.78, 0.84);
        }
      }
    }

    /* 4 · the beat, then circle a card */
    {
      const b = P[4],
        W0 = 0.1,
        WD = 0.12,
        gR = grid.getBoundingClientRect(),
        act = own === 4;
      let prev = home;
      chips.forEach((ch, i) => {
        const u = seg(b, W0 + i * WD, W0 + (i + 1) * WD),
          cR = ch.getBoundingClientRect(),
          lR = lanes[i].getBoundingClientRect(),
          reg = regs[i];
        ch.classList.toggle("used", u >= 0.48);
        const top = lR.top + 6,
          h = lR.height - 12,
          grow = u >= 1 ? 1 : u > 0.86 ? ease(seg(u, 0.86, 1)) : 0;
        reg.style.opacity = u > 0.86 ? "1" : "0";
        reg.style.width = lerp(cR.width, lR.width, grow) + "px";
        if (!act || u <= 0 || u >= 1) {
          if (u >= 1) prev = [lR.left + lR.width * 0.4, lR.top + lR.height / 2];
          return;
        }
        if (u < 0.4) st.cur = arc(prev, [cR.left + cR.width * 0.35, cR.top + cR.height * 0.5], ease(seg(u, 0, 0.4)), 0.18);
        else if (u <= 0.86) {
          const g = ease(seg(u, 0.48, 0.86)),
            lift = seg(u, 0.4, 0.48),
            x = lerp(cR.left, lR.left, g),
            yy = lerp(cR.top, top, g) - 8 * lift,
            hh = lerp(cR.height, h, g);
          carry.c = { cls: "ghost chip c" + i, txt: ch.textContent ?? "", x, y: yy, w: cR.width, h: hh };
          st.cur = [x + cR.width * 0.35, yy + hh * 0.5];
        } else st.cur = [lR.left + lerp(cR.width, lR.width, grow) - 6, lR.top + lR.height / 2];
        st.look = st.cur;
      });
      const pR = play.getBoundingClientRect(),
        pp = seg(b, 0.55, 1),
        playing = b >= 0.55;
      play.style.transform = `scale(${1 - 0.12 * Math.sin(Math.PI * seg(b, 0.52, 0.55))})`;
      playIco.setAttribute("d", playing ? PAUSE : PLAY);
      const lane0 = lanes[0].getBoundingClientRect();
      phead.style.opacity = b >= 0.5 ? "1" : "0";
      phead.style.transform = `translateX(${lane0.left - gR.left + (reduce ? 0.4 : pp) * lane0.width}px)`;
      const beat = pp * 32;
      posLcd.textContent = `${1 + Math.floor(beat / 4)} . ${1 + Math.floor(beat % 4)}`;
      notes.forEach(([n, x]) => n.classList.toggle("hit", playing && pp - x >= 0 && pp - x < 0.03));
      const lv = playing && !reduce ? [Math.exp(-fr(beat * 2) * 5), 0.45 + 0.55 * Math.exp(-fr(beat) * 3), 0.5 + 0.3 * Math.sin(beat * 1.7)] : [0, 0, 0];
      meters.forEach((bars, k) => bars.forEach((bb, j) => (bb.style.background = j < Math.round(lv[k] * 7) ? MCOL[k] : "")));
      specBars.forEach((bb, j) => (bb.style.transform = `scaleY(${playing && !reduce ? 1 + 6.6 * Math.abs(Math.sin(beat * (0.9 + j * 0.23) + j)) * (1 - j / 22) : 1})`));
      ringCard.set(seg(b, 0.66, 0.82));
      if (act) {
        st.want = 1;
        st.ro = Math.min(seg(b, 0.06, 0.1), 1 - seg(b, 0.88, 0.94));
        says(4);
        if (b >= 0.46 && b < 0.58) {
          st.cur = b < 0.52 ? arc(prev, mid(pR), ease(seg(b, 0.46, 0.52)), 0.2) : mid(pR);
          st.look = st.cur;
        }
        if (b >= 0.58) {
          const s0 = ringCard.at(0),
            e = ringCard.at(1),
            o = eo(seg(b, 0.82, 0.88));
          st.cur = b < 0.66 ? arc(mid(pR), s0, ease(seg(b, 0.58, 0.66)), -0.2) : b < 0.82 ? ringCard.at(seg(b, 0.66, 0.82)) : [e[0] + 30 * o, e[1] + 36 * o];
          st.look = st.cur;
          st.curOp = 1 - seg(b, 0.88, 0.93);
        }
        if (b < 0.09) st.cur = null;
      }
    }

    /* 5 · set up on your own plan */
    {
      const p = P[5],
        W0 = 0.16,
        WD = 0.17;
      let prev = home;
      targets.forEach((t, k) => {
        const u = seg(p, W0 + k * WD, W0 + (k + 1) * WD),
          on = u >= 0.72,
          r = t.getBoundingClientRect(),
          tg: P2 = [r.left + r.width * 0.4, r.top + r.height * 0.55];
        t.classList.toggle("on", on);
        if (k === 0) t.textContent = on ? "Signed in" : "Continue with email";
        if (k === 3) hi.style.opacity = String(eo(seg(u, 0.72, 0.9)));
        const ok = sus[k].querySelector(".ok") as HTMLElement,
          o = eo(seg(u, 0.75, 0.95));
        ok.style.opacity = String(o);
        ok.style.transform = `scale(${0.6 + 0.4 * o})`;
        t.style.transform = `scale(${1 - 0.05 * Math.sin(Math.PI * seg(u, 0.6, 0.72))})`;
        if (own === 5 && u > 0 && u < 1) {
          st.cur = u < 0.6 ? arc(prev, tg, ease(seg(u, 0, 0.6)), 0.15) : tg;
          st.look = tg;
        }
        prev = tg;
      });
      if (own === 5) {
        st.want = seg(p, 0.02, 0.06);
        st.ro = Math.min(seg(p, 0.12, 0.16), 1 - seg(p, 0.86, 0.92));
        says(5);
        if (p >= 0.84 && p < 0.9) {
          const r = targets[3].getBoundingClientRect(),
            o = eo(seg(p, 0.84, 0.9));
          st.cur = [r.left + r.width * 0.4 + 24 * o, r.top + r.height * 0.55 + 28 * o];
          st.curOp = 1 - seg(p, 0.86, 0.9);
        }
      }
    }

    /* 6 · where your things go */
    {
      const p = P[6];
      pl.forEach((d, i) => (d.style.top = `calc(${fr(p * 3 + i * 0.5) * 100}% - 6px)`));
      ring6.set(seg(p, 0.3, 0.56));
      if (own === 6) {
        st.want = seg(p, 0.06, 0.1);
        st.ro = Math.min(seg(p, 0.2, 0.24), 1 - seg(p, 0.62, 0.68));
        says(6);
        if (p >= 0.22) {
          const e = ring6.at(1),
            o = eo(seg(p, 0.56, 0.62));
          st.cur = p < 0.3 ? arc(home, ring6.at(0), ease(seg(p, 0.22, 0.3)), 0.2) : p < 0.56 ? ring6.at(seg(p, 0.3, 0.56)) : [e[0] + 30 * o, e[1] + 36 * o];
          st.look = st.cur;
          st.curOp = 1 - seg(p, 0.6, 0.66);
        }
      }
    }

    /* 7 · questions */
    {
      const p = P[7];
      if (SEEK || reduce) q5.open = p >= 0.4;
      else if (p >= 0.4 && !q5auto) {
        q5.open = true;
        q5auto = true;
      }
      if (own === 7) {
        st.want = seg(p, 0.04, 0.08);
        st.ro = Math.min(seg(p, 0.18, 0.22), 1 - seg(p, 0.55, 0.62));
        says(7);
        const r = (q5.querySelector("summary") as HTMLElement).getBoundingClientRect(),
          tg: P2 = [r.left + Math.min(240, r.width * 0.4), r.top + r.height * 0.55];
        if (p >= 0.2) {
          const o = eo(seg(p, 0.42, 0.5));
          st.cur = p < 0.38 ? arc(home, tg, ease(seg(p, 0.2, 0.38)), 0.2) : [tg[0] + 20 * o, tg[1] + 26 * o];
          st.look = tg;
          st.curOp = 1 - seg(p, 0.55, 0.6);
        }
      }
    }

    /* 8 · closing */
    {
      const p = P[8];
      ring8.set(seg(p, 0.32, 0.58));
      if (own === 8) {
        st.want = seg(p, 0.05, 0.1);
        st.ro = Math.min(seg(p, 0.2, 0.24), 1 - seg(p, 0.62, 0.68));
        says(8);
        if (p >= 0.22) {
          const e = ring8.at(1),
            o = eo(seg(p, 0.58, 0.64));
          st.cur = p < 0.32 ? arc(home, ring8.at(0), ease(seg(p, 0.22, 0.32)), -0.2) : p < 0.58 ? ring8.at(seg(p, 0.32, 0.58)) : [e[0] + 30 * o, e[1] + 36 * o];
          st.look = st.cur;
          st.curOp = 1 - seg(p, 0.62, 0.68);
        }
      }
    }

    if (reduce) {
      st.ro = 0;
      st.cur = null;
    }
    if (lit) st.happy = true;
    const e = Z[own] * st.want;

    /* ghosts: the photo or loop being carried */
    const g1 = carry.p,
      g2 = carry.c;
    if (g1) {
      gPhoto.className = g1.cls;
      Object.assign(gPhoto.style, { display: "block", width: g1.w + "px", height: g1.h + "px", transform: `translate(${g1.x}px,${g1.y}px)` });
    } else gPhoto.style.display = "none";
    if (g2) {
      gChip.className = g2.cls;
      gChip.textContent = g2.txt ?? "";
      Object.assign(gChip.style, { display: "flex", width: g2.w + "px", height: g2.h + "px", transform: `translate(${g2.x}px,${g2.y}px)` });
    } else gChip.style.display = "none";

    /* the notch: tight around what it holds */
    const ro = st.ro * Z[own],
      cs = phone ? 1.5 : 2,
      CW = COLS * cs,
      CH = ROWS * cs,
      ci = 1 - ro;
    meas.className = "cap";
    meas.textContent = st.full;
    let tw = meas.offsetWidth;
    meas.className = "lab";
    meas.textContent = st.label;
    tw = Math.max(tw, meas.offsetWidth);
    const Wc = CW + 30,
      Win = 14 + CW + 14 + tw + 30,
      Wout = tw + 56,
      Hin = CH + 12,
      Hout = phone ? 56 : 70;
    const ee = eo(e),
      W = Math.min(vw - 24, lerp(Wc, lerp(Wout, Win, ci), ee)),
      H = lerp(Hout, Hin, Math.max(ci, 1 - ee));
    notch.style.width = W + "px";
    notch.style.height = H + "px";
    np.style.left = lerp(28, 14 + CW + 14, ci) + "px";
    np.style.opacity = String(clamp((e - 0.55) / 0.45));
    lab.textContent = st.label;
    cap.innerHTML = st.full ? typeOut(st.full, st.typedP, true) : "&nbsp;";

    /* the creature: in the notch, or riding beside the pointer */
    const cur = st.cur,
      inN: P2 = [lerp(vw / 2, vw / 2 - W / 2 + 14 + CW / 2, ee), navB + 6 + CH / 2],
      bob = reduce ? 0 : Math.sin(now * 6) * 3;
    const side = cur && cur[0] + CW * 1.2 > vw ? -0.5 : 0.62,
      ride: P2 = cur ? [cur[0] + CW * side, cur[1] - CH * 0.18 + bob] : inN;
    const pos = ro > 0 && ro < 1 ? arc(inN, ride, ease(ro), 0.25) : ro >= 1 ? ride : inN,
      sc = lerp(1, 0.72, ease(ro));
    crea.style.width = CW + "px";
    crea.style.height = CH + "px";
    crea.style.transform = `translate(${pos[0] - CW / 2}px,${pos[1] - CH / 2}px) scale(${sc})`;

    /* the pointer */
    if (cur) {
      cursor.style.opacity = String(st.curOp);
      cursor.style.transform = `translate(${cur[0] - 2}px,${cur[1] - 2}px)`;
    } else cursor.style.opacity = "0";

    /* the face */
    let ox = 0,
      oy = 1,
      eyes = "O";
    const look = st.look || (own > 1 && !cur ? ([vw * 0.4, vh * 0.6] as P2) : null);
    if (look) {
      const dx = look[0] - pos[0],
        dy = look[1] - pos[1];
      ox = Math.round(clamp(dx / 120, -2, 2));
      oy = dy > 60 ? 2 : dy < -60 ? -1 : 1;
    }
    if (st.happy) eyes = "^";
    else if (now % 3.9 < 0.14) eyes = "-";
    face(eyes, st.happy ? 0 : ox, st.happy ? 0 : oy, st.happy);
  }

  // `?t=`: the same scroll a visitor would make, with a pause between sections.
  const DUR = [0, 7.8, 4.2, 3.4, 5.4, 3.6, 2.8, 3.0, 2.8],
    GAP = 0.8;
  function timeline(t: number): { y: number; T: number } {
    let acc = DUR[1];
    if (t < acc) return { y: 0, T: t };
    let prevEnd = 0;
    for (let k = 2; k <= 8; k++) {
      const s = sec(k),
        top = s.offsetTop,
        r = s.offsetHeight - innerHeight;
      if (t < acc + GAP) return { y: lerp(prevEnd, top, ease(seg(t, acc, acc + GAP))), T: 99 };
      acc += GAP;
      if (t < acc + DUR[k] || k === 8) return { y: top + seg(t, acc, acc + DUR[k]) * r, T: 99 };
      acc += DUR[k];
      prevEnd = top + r;
    }
    return { y: 0, T: 99 };
  }

  let raf = 0,
    alive = true;
  const t0 = performance.now();
  const loop = (now: number) => {
    const s = (now - t0) / 1000;
    render(s, s);
    raf = requestAnimationFrame(loop);
  };
  layout();
  addEventListener("resize", layout);
  document.fonts.ready.then(() => {
    if (!alive) return;
    layout();
    if (SEEK) {
      const s = timeline(parseFloat(q.get("t") ?? "") || 0);
      scrollTo(0, s.y);
      render(s.T, s.T);
    } else raf = requestAnimationFrame(loop);
  });

  return () => {
    alive = false;
    cancelAnimationFrame(raf);
    removeEventListener("resize", layout);
    window.removeEventListener("spark:light", onLight);
    q5.querySelector("summary")?.removeEventListener("click", q5click);
    made.forEach((n) => n.parentNode?.removeChild(n));
  };
}
