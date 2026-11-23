"use client";

import { useEffect } from "react";

/* The call on /meetings. Section progress is the scroll position; the first screen plays on time. The caption
   bar at the bottom says who is speaking, and Nova's light at its left end lights only when someone says "Nova".
   Reduced motion (and no JavaScript) shows every end state, as the markup is drawn. `?t=<seconds>` freezes a
   scripted scroll at that moment and `?at=<section>,<progress>` holds one section (both for screenshots). The
   words live in MeetingsLanding; everything this drives is decoration. Ported from the approved mockup (2026-11-24). */

type Ev = [number, number, string, string];
type Cap = { cur: Ev | null; prev: Ev | null; t: number };

export function MeetingsPlayer() {
  useEffect(() => start(), []);
  return null;
}

const WHO: Record<string, string> = { pri: "Priya", dan: "Dan", mei: "Mei", jo: "Jo", sam: "Sam", nova: "Nova" };
const ADDR = "agents@your-business.thelightbulb.company";
const E1: Ev[] = [
  [0.6, 2.3, "pri", "So we close on Saturdays from January?"],
  [1.4, 2.9, "dan", "Yes, and I can tell the team."],
  [2.1, 3.3, "mei", "Fine by me, as long as the"],
  [2.6, 3.7, "jo", "Wait, which January?"],
  [4.1, 5.6, "pri", "Nova, what did we just decide about Saturdays?"],
  [5.9, 7.8, "nova", "You agreed to close on Saturdays from January. Dan will tell the team."],
];
const E3: Ev[] = [
  [0.04, 0.34, "nova", "Hi, I'm Nova. This meeting is recorded and notes are saved."],
  [0.36, 0.58, "nova", "Say my name to wake me. To opt out, say: Nova, stop transcribing me."],
  [0.64, 0.76, "sam", "Nova, stop transcribing me."],
  [0.8, 0.95, "nova", "Okay, Sam. I've stopped transcribing you."],
];
const NOVA = "<b class='nv'>Nova</b>";

function start(): (() => void) | undefined {
  if (typeof window.matchMedia !== "function" || !document.fonts) return; // jsdom
  if (matchMedia("(prefers-reduced-motion: reduce)").matches) return; // the markup is the end state
  const $ = (id: string) => document.getElementById(id) as HTMLElement;
  const root = document.querySelector<HTMLElement>(".lm");
  if (!root || !$("s1")) return;
  root.classList.add("js");

  const q = new URLSearchParams(location.search);
  const clamp = (v: number, a = 0, b = 1) => Math.min(b, Math.max(a, v)),
    seg = (v: number, a: number, b: number) => clamp((v - a) / (b - a)),
    lerp = (a: number, b: number, t: number) => a + (b - a) * t,
    ease = (t: number) => (t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2);
  const S: HTMLElement[] = [root, ...[1, 2, 3, 4, 5, 6, 7, 8, 9].map((i) => $("s" + i))];
  const tiles = (id: string) =>
    Object.fromEntries([...$(id).querySelectorAll<HTMLElement>(".tile")].map((t) => [t.dataset.k!, t]));
  const T1 = tiles("c1"),
    T2 = tiles("c2"),
    T3 = tiles("c3");
  const rollP = [...$("roll").children] as HTMLElement[];
  const FEED = rollP.map((p) => [p.dataset.k ?? "", p.textContent!.slice(p.querySelector("b")!.textContent!.length)]);
  const lis5 = [...$("doc").querySelectorAll("li")],
    ai6 = [...root.querySelectorAll<HTMLElement>(".ai")];
  const bar = $("bar"),
    bc = $("bc"),
    bp = $("bp"),
    foot = root.querySelector<HTMLElement>("footer");

  const captions = (ev: Ev[], t: number): Cap => {
    let cur: Ev | null = null,
      prev: Ev | null = null;
    for (const e of ev)
      if (t >= e[0]) {
        prev = cur;
        cur = e;
      }
    return { cur, prev, t };
  };
  const type = (txt: string, p: number) => [...txt].slice(0, Math.round(txt.length * clamp(p))).join("");
  const talk = (map: Record<string, HTMLElement>, ev: Ev[], t: number) => {
    for (const k in map) map[k].classList.toggle("talk", k !== "nova" && ev.some((e) => e[2] === k && t >= e[0] && t < e[1]));
  };
  const line = (e: Ev | null, typed?: string) =>
    e ? `<b class="${e[2] === "nova" ? "nv" : ""}">${WHO[e[2]] ?? e[2]}</b>${typed ?? e[3]}` : "";
  let last = "";
  const paint = (c: Cap | null, lit: boolean, quiet?: string) => {
    let cur = "",
      prev = "";
    if (quiet) cur = quiet;
    else if (c && c.cur) {
      const [f, to] = c.cur,
        p = seg(c.t, f, f + (to - f) * 0.8);
      cur = line(c.cur, type(c.cur[3], p)) + (p < 1 ? "<i></i>" : "");
      prev = line(c.prev);
    }
    const key = cur + "|" + prev + "|" + lit + "|" + !!quiet;
    if (key === last) return;
    last = key;
    bc.innerHTML = cur;
    bc.className = quiet ? "quiet" : "";
    bp.innerHTML = prev;
    bar.classList.toggle("lit", lit);
  };

  function render(T: number) {
    const vh = innerHeight,
      y = scrollY;
    // Pinned (wide screens): progress through the sticky frame. Unpinned (narrow screens): progress as the section
    // scrolls up from the bottom of the screen.
    const pr = (k: number) => {
      const s = S[k],
        r = s.offsetHeight - vh;
      return r > vh * 0.5 ? clamp((y - s.offsetTop) / r) : clamp((y + vh * 0.85 - s.offsetTop) / (s.offsetHeight * 0.9));
    };
    let own = 1;
    for (let k = 2; k <= 9; k++) {
      const r = S[k].getBoundingClientRect();
      if (r.top < vh * 0.5 && r.bottom > vh * 0.5) own = k;
    }
    if (y < vh * 0.4) own = 1;
    bar.classList.toggle("off", !!foot && foot.getBoundingClientRect().top < vh - 110);

    /* 1: the call, people over each other, then the name */
    talk(T1, E1, T);
    const lit1 = T >= 4.35 && T < 8.3;
    T1.nova.classList.toggle("lit", lit1);
    $("d1").classList.toggle("on", T >= 8.1);
    $("f1").classList.toggle("on", T >= 8.5);
    if (own === 1) paint(T < 0.6 ? null : captions(E1, T), lit1, T < 0.6 ? "Nova is listening for its name." : undefined);

    /* 2: the invite */
    {
      const p = pr(2),
        tp = seg(p, 0.06, 0.36),
        g = $("gfield");
      g.classList.toggle("focus", p > 0.04 && p < 0.4);
      g.innerHTML = p < 0.4 ? type(ADDR, tp) + (p > 0.04 ? "<i></i>" : "") : "Add guests";
      $("nchip").classList.toggle("on", p >= 0.4);
      $("save").classList.toggle("saved", p >= 0.5);
      $("save").textContent = p >= 0.5 ? "Saved" : "Save";
      $("clk").textContent = p >= 0.6 ? "Thursday, 10:00" : "Wednesday, 16:20";
      T2.nova.classList.toggle("in", p >= 0.68);
      $("toast").classList.toggle("on", p >= 0.7 && p < 0.97);
      talk(T2, [[0.62, 0.8, "pri", ""], [0.78, 1, "dan", ""]], p);
      if (own === 2)
        paint(
          null,
          false,
          p >= 0.7 ? "<b>Nova</b>joined Weekly planning." : p >= 0.5 ? "Invite saved. Nova will join on Thursday at 10:00." : "Nova isn't in a call.",
        );
    }

    /* 3: the announcement and the opt-out */
    {
      const p = pr(3);
      talk(T3, E3, p);
      const lit = (p >= 0.04 && p < 0.6) || (p >= 0.66 && p < 0.97);
      T3.nova.classList.toggle("lit", lit);
      T3.sam.querySelector(".badge")!.classList.toggle("on", p >= 0.93);
      if (own === 3) paint(captions(E3, p), lit);
    }

    /* 4: chatter, then the name */
    {
      const p = pr(4),
        roll = $("roll"),
        feed = roll.parentElement!;
      const target = rollP[7].offsetTop + rollP[7].offsetHeight - feed.clientHeight * 0.62;
      const end = rollP[9].offsetTop + rollP[9].offsetHeight - feed.clientHeight * 0.86;
      const yy =
        lerp(0, Math.max(0, target), ease(seg(p, 0.02, 0.6))) + lerp(0, Math.max(0, end - target), ease(seg(p, 0.62, 0.8)));
      roll.style.transform = `translateY(${-yy}px)`;
      rollP[9].classList.toggle("on", p >= 0.8);
      const lit = p >= 0.66 && p < 0.98;
      $("n4").classList.toggle("lit", lit);
      $("st4").textContent = lit ? "Named" : "Quiet";
      if (own === 4) {
        const i = Math.min(7, Math.floor(seg(p, 0.02, 0.62) * 8));
        const ev: Ev[] = FEED.slice(0, 8).map(([k, t], j) => [j, j + 1, k, t]);
        if (p < 0.64) paint({ cur: ev[i], prev: ev[i - 1] || null, t: i + 0.99 }, false);
        else
          paint(
            captions(
              [
                [0.64, 0.78, "pri", FEED[8][1]],
                [0.8, 0.95, "nova", FEED[9][1]],
              ],
              p,
            ),
            lit,
          );
      }
    }

    /* 5: the notes fill in */
    {
      const p = pr(5);
      lis5.forEach((l, i) => l.classList.toggle("on", p >= 0.06 + i * (0.8 / lis5.length)));
      if (own === 5) paint(null, p > 0.9, p > 0.9 ? NOVA + "Notes saved for Weekly planning." : "The call ended. Nova is writing up.");
    }

    /* 6: follow-ups and the approval */
    {
      const p = pr(6),
        cur = $("cur6"),
        ok = $("ok6"),
        nd = $("needs");
      $("sw").classList.toggle("on", p >= 0.08);
      ai6.forEach((a, i) => a.classList.toggle("on", p >= 0.18 + i * 0.1));
      nd.classList.toggle("on", p >= 0.5);
      const u = seg(p, 0.62, 0.82);
      cur.style.opacity = p >= 0.6 && p < 0.95 ? "1" : "0";
      if (ok.offsetWidth) {
        const x0 = nd.clientWidth * 0.8,
          y0 = nd.clientHeight * 1.05,
          x1 = ok.offsetLeft + ok.offsetWidth * 0.55,
          y1 = ok.offsetTop + ok.offsetHeight * 0.55;
        cur.style.left = lerp(x0, x1, ease(u)) + "px";
        cur.style.top = lerp(y0, y1, ease(u)) + "px";
      }
      nd.classList.toggle("ok", p >= 0.86);
      if (own === 6)
        paint(
          null,
          p >= 0.5 && p < 0.86,
          p >= 0.86
            ? "<b>You</b>approved the email to Harbor Glass."
            : p >= 0.5
              ? NOVA + "The quote email is waiting for your approval."
              : NOVA + "Three action items, three follow-ups.",
        );
    }
    if (own >= 7)
      paint(null, own === 9, own === 9 ? NOVA + "Notes saved. See you at the next one." : "Nova is listening for its name.");
  }

  /* driving: a scripted scroll for ?t=, one held moment for ?at=, else live */
  const DUR = [0, 8.8, 3.4, 4.6, 4.6, 3.2, 4.0, 1.8, 1.6, 2.2],
    GAP = 0.7;
  const timeline = (t: number) => {
    const vh = innerHeight,
      max = document.documentElement.scrollHeight - vh;
    let acc = DUR[1];
    if (t < acc) return { y: 0, T: t };
    let prevEnd = 0;
    for (let k = 2; k <= 9; k++) {
      const s = S[k],
        top = Math.min(s.offsetTop, max),
        r = Math.max(0, Math.min(s.offsetHeight - vh, max - top));
      if (t < acc + GAP) return { y: lerp(prevEnd, top, ease(seg(t, acc, acc + GAP))), T: 99 };
      acc += GAP;
      if (t < acc + DUR[k] || k === 9) return { y: top + seg(t, acc, acc + DUR[k]) * r, T: 99 };
      acc += DUR[k];
      prevEnd = top + r;
    }
    return { y: 0, T: 99 };
  };
  let raf = 0;
  const go = () => {
    if (q.has("at")) {
      const [k, p] = (q.get("at") ?? "").split(",").map(Number);
      const s = S[k] ?? S[1];
      scrollTo(0, k > 1 ? s.offsetTop + (p || 0) * Math.max(0, s.offsetHeight - innerHeight) : 0);
      render(k > 1 ? 99 : p || 99);
    } else if (q.has("t")) {
      const s = timeline(parseFloat(q.get("t") ?? "") || 0);
      scrollTo(0, s.y);
      render(s.T);
    } else {
      const t0 = performance.now();
      const loop = (n: number) => {
        render((n - t0) / 1000);
        raf = requestAnimationFrame(loop);
      };
      raf = requestAnimationFrame(loop);
    }
  };
  let live = true;
  void document.fonts.ready.then(() => live && go());

  return () => {
    live = false;
    cancelAnimationFrame(raf);
    root.classList.remove("js");
  };
}
