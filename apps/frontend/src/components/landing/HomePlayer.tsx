"use client";

import Link from "next/link";
import { useEffect } from "react";

import { trackSparkDownload, useOnPhone } from "@/app/os/SparkDownload";
import { captureSignupCta } from "@/lib/signupFlow";

import { CALLS, STEPS } from "./homeDemo";

/* The home page's doors, tracked like the rest of the site's. */
const PHONE_LABEL = "Set up Lightbulb Phone";
export function PhoneSignupLink() {
  return (
    <Link
      className="btn"
      href="/sign-up"
      onClick={() => captureSignupCta({ ctaId: "hub_sign_up", label: PHONE_LABEL, surface: "hero", destination: "/sign-up" })}
    >
      {PHONE_LABEL}
    </Link>
  );
}

// A phone cannot install a Mac app, so there the button goes to /os's "open this on your Mac" line (as the menu does).
export function SparkDownloadLink() {
  const onPhone = useOnPhone();
  return (
    <a className="btn" href={onPhone ? "/os#get" : "/download/spark"} onClick={(e) => !onPhone && trackSparkDownload("home", e.currentTarget)}>
      Download for Mac
    </a>
  );
}

export function FounderLink() {
  return (
    <Link
      className="btn"
      href="/request-setup"
      onClick={() =>
        captureSignupCta({ ctaId: "hub_founder", label: "Talk to a founder", surface: "hub_closing", destination: "/request-setup" })
      }
    >
      Talk to a founder
    </Link>
  );
}

/* The window on /: the turning word, the tabs (an ARIA tablist with arrow keys) and the three plays. Ported from
   the approved mockup. Reduced motion skips every wait, so each play lands on its end state; without JavaScript
   the server markup is the resting state. `?tab=spark|phone|meetings&play=1` (Spark: `play=photos|folder`) and
   `&t=<ms>` freeze a moment, for screenshots. */
export function HomePlayer() {
  useEffect(() => start(), []);
  return null;
}

function start(): (() => void) | undefined {
  if (typeof window.matchMedia !== "function") return; // jsdom
  const $ = (id: string) => document.getElementById(id) as HTMLElement;
  if (!$("win")) return;
  const Q = new URLSearchParams(location.search);
  const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
  let frozen = false;
  let dead = false;
  const timers: number[] = [];
  const later = (fn: () => void, ms: number) => timers.push(window.setTimeout(fn, ms));
  if (Q.has("t")) {
    document.documentElement.classList.add("lh-cap");
    later(() => (frozen = true), +Q.get("t")!);
  }
  // Every timed step goes through wait(): reduced motion skips to the end; a capture freeze or unmount stops it.
  const wait = (ms: number) =>
    new Promise<void>((r) => {
      if (frozen || dead) return;
      later(() => !frozen && !dead && r(), reduce ? 0 : ms);
    });
  const off: (() => void)[] = [];
  const on = <K extends keyof HTMLElementEventMap>(el: EventTarget, ev: K, fn: (e: HTMLElementEventMap[K]) => void) => {
    el.addEventListener(ev, fn as EventListener);
    off.push(() => el.removeEventListener(ev, fn as EventListener));
  };

  /* the one orchestrated moment: the word turns while the window rises (CSS) */
  const word = $("word");
  const swap = async (w: string) => {
    if (reduce) return void (word.textContent = w);
    word.classList.add("out");
    await wait(220);
    word.textContent = w;
    word.classList.remove("out");
    word.classList.add("pre");
    void word.offsetWidth;
    word.classList.remove("pre");
  };
  void (async () => {
    if (Q.has("word")) return void (word.textContent = Q.get("word"));
    if (reduce) return;
    await wait(500);
    for (const w of ["business", "computer", "life"]) {
      await wait(1900);
      await swap(w);
    }
  })();

  /* tabs */
  const tabs = [...document.querySelectorAll<HTMLElement>(".lh [role=tab]")];
  const select = (tab: HTMLElement, focus?: boolean) => {
    tabs.forEach((t) => {
      const sel = t === tab;
      t.setAttribute("aria-selected", String(sel));
      t.tabIndex = sel ? 0 : -1;
      $(t.getAttribute("aria-controls")!).hidden = !sel;
    });
    if (focus) tab.focus();
    if (tab.id === "t-spark") drawSky();
  };
  tabs.forEach((t, i) => {
    on(t, "click", () => select(t));
    on(t, "keydown", (e) => {
      const n = ({ ArrowRight: i + 1, ArrowLeft: i - 1, Home: 0, End: tabs.length - 1 } as Record<string, number>)[e.key];
      if (n === undefined) return;
      e.preventDefault();
      select(tabs[(n + tabs.length) % tabs.length], true);
    });
  });

  /* PHONE: one call, played line by line, then written into the book */
  const callsEl = $("calls"), linesEl = $("lines"), didEl = $("did"), rowsEl = $("rows");
  const callBtns = [...callsEl.querySelectorAll<HTMLButtonElement>(".call")];
  const pBtn = $("playCall") as HTMLButtonElement;
  let cur = 0, pPlaying = false;
  const esc = (s: string) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;");
  function book(i: number, written: boolean) {
    const b = CALLS[i].book;
    $("bDay").textContent = b.day;
    $("bChair").textContent = b.chair;
    rowsEl.innerHTML = b.rows
      .map(([t, w]) => {
        const hit = t === b.at;
        const cls = hit ? ` new${written ? " done" : ""}` : "";
        return `<div class="row${hit && written ? " hit" : ""}"><time>${t}</time><span class="w${cls}">${esc(hit ? b.text! : w)}</span></div>`;
      })
      .join("");
    $("sticky").classList.toggle("hide", !(b.sticky && written));
  }
  function display(state: string, who: string, timer: string, live: boolean) {
    $("dState").textContent = state;
    $("dWho").textContent = who;
    $("dTimer").textContent = timer;
    $("dDot").classList.toggle("on", live);
  }
  function showCall(i: number, full: boolean) {
    cur = i;
    const c = CALLS[i];
    callBtns.forEach((b, j) => b.setAttribute("aria-pressed", String(j === i)));
    $("dClock").textContent = c.time;
    $("dDay").textContent = c.day;
    display(full ? "Call ended" : "Ringing · Salon line", c.who, full ? c.len : "", false);
    linesEl.innerHTML = c.lines
      .map(
        ([w, t]) =>
          `<div class="ln${w === "Nova" ? " nova" : ""}${full ? "" : " hide"}"><b><span class="livedot"></span>${w}</b><span>${esc(t)}</span></div>`,
      )
      .join("");
    didEl.innerHTML = `<p>What Nova did</p><ul>${c.did.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>`;
    didEl.classList.toggle("hide", !full);
    book(i, full);
  }
  callBtns.forEach((b, i) =>
    on(b, "click", () => {
      if (pPlaying) return;
      showCall(i, true);
      pBtn.textContent = "Play this call";
    }),
  );
  const fmt = (n: number) => `${Math.floor(n / 60)}:${String(n % 60).padStart(2, "0")}`;
  async function playCall() {
    if (pPlaying) return;
    pPlaying = true;
    pBtn.disabled = true;
    pBtn.textContent = "Playing";
    const c = CALLS[cur];
    showCall(cur, false);
    display("Ringing · Salon line", c.who, "", true);
    await wait(1100);
    let s = 0;
    const lines = [...linesEl.children];
    for (const el of lines) {
      lines.forEach((x) => x.classList.remove("now"));
      el.classList.remove("hide");
      el.classList.add("now");
      display("On a call", c.who, fmt(s), true);
      const n = (el.textContent ?? "").length;
      s += Math.round(n / 9) + 2;
      await wait(700 + n * 28);
    }
    lines.forEach((x) => x.classList.remove("now"));
    display("Call ended", c.who, c.len, false);
    didEl.classList.remove("hide");
    await wait(400);
    book(cur, false);
    void rowsEl.offsetWidth;
    const nw = rowsEl.querySelector(".w.new");
    if (nw) {
      nw.classList.add("done");
      nw.parentElement!.classList.add("hit");
    }
    if (c.book.sticky) $("sticky").classList.remove("hide");
    pPlaying = false;
    pBtn.disabled = false;
    pBtn.textContent = "Play it again";
  }
  on(pBtn, "click", playCall);

  /* MEETINGS: five people talk, Nova answers when named, the notes fill */
  const lists = ["nNotes", "nDec", "nFol"];
  const empty = () => lists.forEach((id) => ($(id).innerHTML = '<li class="empty">Nothing yet</li>'));
  const add = ([id, text]: [string, string]) => {
    const ul = $(id);
    ul.querySelector(".empty")?.remove();
    const li = document.createElement("li");
    li.textContent = text;
    li.className = "fresh";
    ul.appendChild(li);
  };
  const mBtn = $("playMeet") as HTMLButtonElement, cap = $("cap"), capbar = $("capbar");
  const tiles = [...document.querySelectorAll(".lh .tile")];
  let mPlaying = false;
  async function playMeet() {
    if (mPlaying) return;
    mPlaying = true;
    mBtn.disabled = true;
    mBtn.textContent = "Playing";
    empty();
    for (const [k, name, said, note] of STEPS) {
      tiles.forEach((t) => t.classList.remove("talk"));
      $(`m-${k}`).classList.add("talk");
      capbar.classList.toggle("lit", k === "nova");
      cap.innerHTML = `<b>${name}:</b> ${esc(said)}`;
      await wait(900 + said.length * 34);
      if (note) add(note);
    }
    tiles.forEach((t) => t.classList.remove("talk"));
    capbar.classList.remove("lit");
    cap.innerHTML = '<span class="idle">Nova is listening for its name.</span>';
    mPlaying = false;
    mBtn.disabled = false;
    mBtn.textContent = "Play it again";
  }
  on(mBtn, "click", playMeet);

  /* SPARK: the creature (as SparkPlayer draws it on /os) */
  const B8 = [0,32,8,40,2,34,10,42,48,16,56,24,50,18,58,26,12,44,4,36,14,46,6,38,60,28,52,20,62,30,54,22,3,35,11,43,1,33,9,41,51,19,59,27,49,17,57,25,15,47,7,39,13,45,5,37,63,31,55,23,61,29,53,21].map((r) => (r + 0.5) / 64); // prettier-ignore
  const bay = (x: number, y: number) => B8[(y & 7) * 8 + (x & 7)];
  type C3 = [number, number, number];
  const hex = (h: string): C3 => {
    const n = parseInt(h.slice(1), 16);
    return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
  };
  const mix = (a: C3, b: C3, t: number): C3 => [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t];
  const SPK = hex("#6e8fc4"), DEEP = hex("#1b2c4a"), WHT = hex("#ffffff"), INK = hex("#080a12"), LIGHT = hex("#f4f8ff");
  const EYE: Record<string, { dx: number[]; dy: number; g: string[] }> = {
    O: { dx: [-13, 5], dy: -5, g: ["..rrrr..", ".rr##rr.", "rr#o##rr", "r#ooo##r", "r##o###r", "r######r", "r####o#r", "rr####rr", ".rr##rr.", "..rrrr.."] },
    "^": { dx: [-14, 4], dy: -3, g: ["..rrrrrr..", ".rr####rr.", "rr######rr", "r###rr###r", "r##rrrr##r", "rrrr..rrrr"] },
  };
  const COLS = 56, ROWS = 52, CR = { x: 28, y: 27, R: 23 };
  const cc = ($("crea") as HTMLCanvasElement).getContext("2d");
  let face: (eyes: string, full: boolean) => void = () => {};
  if (cc) {
    const blob = new Path2D();
    let st = 4;
    const next = () => (st = (Math.imul(st, 1664525) + 1013904223) >>> 0) / 4294967296;
    const harm = [1, 2, 3, 4, 5, 7].map((k) => [k, (next() - 0.5) * (k === 2 ? 0.16 : 0.08), next() * 6.28]);
    for (let i = 0; i <= 120; i++) {
      const th = (i / 120) * 2 * Math.PI;
      let rr = CR.R;
      harm.forEach((h) => (rr += CR.R * h[1] * Math.sin(h[0] * th + h[2])));
      const qx = CR.x + rr * Math.cos(th), qy = CR.y + (rr * Math.sin(th)) / 1.05;
      if (i) blob.lineTo(qx, qy);
      else blob.moveTo(qx, qy);
    }
    blob.closePath();
    const m = new Uint8Array(COLS * ROWS);
    for (let y = 0; y < ROWS; y++) for (let x = 0; x < COLS; x++) m[y * COLS + x] = cc.isPointInPath(blob, x + 0.5, y + 0.5) ? 1 : 0;
    const dist = (inside: boolean) => {
      const big = 1e6, D = 1.414, d = new Float32Array(COLS * ROWS);
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
    const bIn = dist(true), bOut = dist(false);
    face = (eyes, full) => {
      const img = new ImageData(COLS, ROWS), px = img.data;
      const put = (x: number, y: number, c: C3) => {
        if (x < 0 || y < 0 || x >= COLS || y >= ROWS) return;
        const k = (y * COLS + x) * 4;
        px[k] = c[0]; px[k + 1] = c[1]; px[k + 2] = c[2]; px[k + 3] = 255; // prettier-ignore
      };
      const pick = (r: C3[], t: number, b: number) => {
        t = Math.max(0, Math.min(0.999, t));
        const f = t * (r.length - 1), lo = Math.floor(f);
        return f - lo > b ? r[Math.min(lo + 1, r.length - 1)] : r[lo];
      };
      const ramp = [mix(DEEP, SPK, 0.28), mix(DEEP, SPK, 0.55), mix(DEEP, SPK, 0.8), SPK, mix(SPK, WHT, 0.35)];
      const halo = [mix(DEEP, SPK, 0.12), mix(DEEP, SPK, 0.22)], hc = CR.R * 0.1;
      for (let i = 0; i < COLS * ROWS; i++) {
        const x = i % COLS, y = (i / COLS) | 0, b = bay(x, y);
        if (m[i]) {
          let tt = Math.min(1, bIn[i] / (0.8 * CR.R));
          tt = tt * 0.75 + 0.25 * Math.max(0, (-(x - CR.x) - (y - CR.y)) / (1.6 * CR.R)) + (full ? 0.08 : 0);
          put(x, y, pick(ramp, tt, b));
        } else if (bOut[i] < hc) {
          const h = 1 - bOut[i] / hc;
          if (h * 1.1 > b) put(x, y, h > 0.55 && b < h - 0.4 ? halo[1] : halo[0]);
        }
      }
      const E = EYE[eyes], ex = Math.round(CR.x), ey = Math.round(CR.y - CR.R * 0.06), rim = mix(SPK, WHT, 0.6);
      E.dx.forEach((ddx) =>
        E.g.forEach((row, ry) => {
          for (let cx = 0; cx < row.length; cx++) {
            const ch = row[cx];
            if (ch !== ".") put(ex + ddx + cx, ey + E.dy + ry, ch === "#" ? INK : ch === "o" ? LIGHT : rim);
          }
        }),
      );
      cc.clearRect(0, 0, COLS, ROWS);
      cc.putImageData(img, 0, 0);
    };
    face("O", false);
  }

  /* the night sky: dithered night ramp, stars and pixel hills, drawn at 1/4 size and scaled up */
  const NIGHT = ["#060b15", "#0a1322", "#0f1a2b", "#15233b"].map(hex);
  function drawSky() {
    const cv = $("sky") as HTMLCanvasElement, r = cv.getBoundingClientRect(), g = cv.getContext("2d");
    if (!r.width || !g) return;
    const W = Math.ceil(r.width / 4), H = Math.ceil(r.height / 4);
    cv.width = W;
    cv.height = H;
    const img = g.createImageData(W, H), p = img.data;
    let st = 9;
    const rnd = () => (st = (Math.imul(st, 1664525) + 1013904223) >>> 0) / 4294967296;
    const hill = (x: number, a: number, f: number, ph: number, base: number) => base + a * Math.sin(x * f + ph) + a * 0.5 * Math.sin(x * f * 2.3 + ph * 1.7);
    const back = mix(DEEP, SPK, 0.08), front = mix(DEEP, SPK, 0.22), glow = hex("#f0c34e"), star = hex("#fffae6");
    for (let y = 0; y < H; y++)
      for (let x = 0; x < W; x++) {
        const b = bay(x, y), t = (y / H) * (NIGHT.length - 1), lo = Math.min(NIGHT.length - 2, Math.floor(t));
        let c = t - lo > b ? NIGHT[lo + 1] : NIGHT[lo];
        const gx = (x - W * 0.42) / (W * 0.3), gy = (y - H * 0.78) / (H * 0.22), gd = gx * gx + gy * gy;
        if (gd < 1 && (1 - gd) * 0.35 > b) c = mix(c, glow, 0.35);
        const h1 = hill(x, H * 0.04, 0.05, 1.2, H * 0.74), h2 = hill(x, H * 0.05, 0.035, 3.1, H * 0.84);
        if (y > h1) c = y - h1 < 2 && b > 0.5 ? mix(back, SPK, 0.25) : back;
        if (y > h2) c = y - h2 < 2 && b > 0.5 ? mix(front, SPK, 0.3) : front;
        const k = (y * W + x) * 4;
        p[k] = c[0]; p[k + 1] = c[1]; p[k + 2] = c[2]; p[k + 3] = 255; // prettier-ignore
      }
    for (let i = 0; i < (W * H) / 520; i++) {
      const x = (rnd() * W) | 0, y = (rnd() * H * 0.66) | 0, k = (y * W + x) * 4, c = rnd() < 0.25 ? glow : star;
      p[k] = c[0]; p[k + 1] = c[1]; p[k + 2] = c[2]; // prettier-ignore
    }
    g.putImageData(img, 0, 0);
  }
  on(window, "resize", () => {
    if (!$("p-spark").hidden) drawSky();
  });

  /* Spark doing it: the pointer clicks through the two asks */
  const sk = $("sk"), notch = $("notch"), ptr = $("pointer");
  const say = (lab: string, txt: string) => {
    notch.classList.add("talk");
    $("nLab").textContent = lab;
    $("nTxt").textContent = txt;
  };
  const at = (el: Element) => {
    const a = el.getBoundingClientRect(), b = sk.getBoundingClientRect();
    return [a.left - b.left + a.width * 0.55, a.top - b.top + a.height * 0.5];
  };
  const move = async (el: Element) => {
    const [x, y] = at(el);
    ptr.style.opacity = "1";
    ptr.style.transform = `translate(${x}px,${y}px)`;
    await wait(650);
  };
  const click = async () => {
    ptr.classList.add("click");
    await wait(140);
    ptr.classList.remove("click");
  };
  function resetDesk() {
    $("chat").querySelectorAll(".sent,.deliv").forEach((e) => e.remove());
    $("strip").querySelectorAll(".ph").forEach((e) => e.classList.remove("picked"));
    $("files").querySelectorAll<HTMLElement>(".file").forEach((f) => {
      f.classList.remove("gone", "lift");
      f.style.transform = "";
    });
    $("folder").classList.remove("show");
    $("fIn").textContent = "Empty";
    $("fCount").textContent = "3 items";
    face("O", false);
  }
  const RUN: Record<string, () => Promise<void>> = {
    async photos() {
      say("Working", "Picking today's beach photos");
      const ph = [...$("strip").children];
      for (const i of [0, 2, 3]) {
        await move(ph[i]);
        await click();
        ph[i].classList.add("picked");
        await wait(150);
      }
      const send = $("send");
      await move(send);
      await click();
      send.classList.add("press");
      await wait(120);
      send.classList.remove("press");
      const sent = document.createElement("span");
      sent.className = "sent";
      sent.innerHTML = '<span class="ph p1"></span><span class="ph p3"></span><span class="ph p4"></span>';
      $("chat").appendChild(sent);
      ph.forEach((p) => p.classList.remove("picked"));
      await wait(600);
      const d = document.createElement("span");
      d.className = "deliv";
      d.textContent = "Delivered";
      $("chat").appendChild(d);
      say("Done", "Three photos sent to Sam.");
    },
    async folder() {
      say("Working", "Making a folder called Receipts");
      const fo = $("folder");
      await move($("files"));
      await click();
      fo.classList.add("show");
      await wait(350);
      const files = [...$("files").querySelectorAll<HTMLElement>(".file")];
      let n = 0;
      for (const f of files) {
        await move(f);
        await click();
        f.classList.add("lift");
        const a = f.getBoundingClientRect(), b = fo.getBoundingClientRect();
        f.style.transform = `translate(${b.left - a.left + 4}px,${b.top - a.top - 6}px) scale(.5)`;
        await move(fo);
        f.classList.add("gone");
        n++;
        $("fIn").textContent = n + (n === 1 ? " item" : " items");
        $("fCount").textContent = `${3 - n + 1} items`;
      }
      say("Done", "Three PDFs are in Receipts.");
    },
  };
  const asks = [...document.querySelectorAll<HTMLButtonElement>(".lh .ask")];
  let sBusy = false;
  async function run(btn: HTMLButtonElement) {
    if (sBusy) return;
    sBusy = true;
    asks.forEach((b) => b.setAttribute("aria-pressed", String(b === btn)));
    resetDesk();
    face("O", true);
    say("Heard", (btn.textContent ?? "").replace(/[“”]/g, ""));
    await wait(1300);
    await RUN[btn.dataset.k!]();
    face("^", false);
    ptr.style.opacity = "0";
    sBusy = false;
  }
  asks.forEach((b) => on(b, "click", () => run(b)));

  select(tabs[0]); // draws the sky for Spark, the tab shown on load

  /* capture hooks */
  const tab = Q.get("tab");
  if (tab && $(`t-${tab}`)) {
    select($(`t-${tab}`));
    if (Q.has("play"))
      later(() => {
        if (tab === "phone") void playCall();
        if (tab === "meetings") void playMeet();
        if (tab === "spark") void run(asks[Q.get("play") === "folder" ? 1 : 0]);
      }, 700);
  }

  return () => {
    dead = true;
    timers.forEach(clearTimeout);
    off.forEach((f) => f());
    document.documentElement.classList.remove("lh-cap");
  };
}
