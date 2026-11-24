"use client";

import { useEffect } from "react";

/* The day on /phone. Section progress is the scroll position; the first screen plays on time. The room's light
   moves from cream morning to midnight, "the line" under the menu says who is speaking, and each pinned
   section's demo moves with it. Reduced motion shows every end state. `?t=<seconds>` freezes a scripted
   scroll at that moment and `?at=<section>,<progress>` holds one section (both for screenshots). The words
   and buttons live in PhoneLanding; everything this drives is decoration. Ported from the approved mockup (2026-11-24). */

type Step = [number, string, string, (number | null)?, (number | null)?, string?];

export function PhonePlayer() {
  useEffect(() => start(), []);
  return null;
}

function start(): (() => void) | undefined {
  if (typeof window.matchMedia !== "function" || !document.fonts) return; // jsdom
  const $ = (id: string) => document.getElementById(id) as HTMLElement;
  const root = document.querySelector<HTMLElement>(".lp");
  if (!root || !$("s1")) return;
  root.classList.add("js");

  const q = new URLSearchParams(location.search);
  const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const clamp = (v: number, a = 0, b = 1) => Math.max(a, Math.min(b, v)),
    seg = (v: number, a: number, b: number) => clamp((v - a) / (b - a)),
    lerp = (a: number, b: number, t: number) => a + (b - a) * t;
  const ease = (t: number) => (t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2),
    eo = (t: number) => 1 - Math.pow(1 - t, 3);
  const hex = (h: string) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16));
  const mixc = (a: string, b: string, t: number) => {
    const A = hex(a),
      B = hex(b);
    return "#" + A.map((v, i) => Math.round(lerp(v, B[i], clamp(t))).toString(16).padStart(2, "0")).join("");
  };
  const S: HTMLElement[] = [root, ...["s1", "s2", "s3", "s4", "s5", "s6", "s7", "faq", "s9"].map($)];

  /* --- the week for section 2 --- */
  const wk = $("wk");
  const OTHER: [number, number, number, string][] = [
    [0, 0, 1, "Kim, cut"], [0, 3, 1.5, "Ava, lashes"], [2, 1, 1, "Ben, fade"], [2, 4, 1, "Rosa, nails"], [3, 0, 1, "Lee, brows"],
    [4, 2, 2, "Nia, color"], [5, 0, 1, "Dana, cut"], [5, 1, 1, "Ray, trim"], [1, 4, 1, "Tom, fade"], [3, 5, 1, "Ivy, gloss"],
  ];
  const add = (cls: string, text: string) => {
    const e = document.createElement("div");
    e.className = cls;
    e.textContent = text;
    wk.appendChild(e);
    return e;
  };
  const evs = OTHER.map(([d, h, len, t]) => [add("ev", t), d, h, len] as const);
  const was = add("ev was", "Tuesday 11:00");
  const pr = add("ev pr", "Priya, color");
  let G: { x0: number; cw: number; ch: number; y0: number } | null = null;
  const gridGeo = () => {
    const c = wk.querySelector<HTMLElement>(".cell")!,
      hr = wk.querySelector<HTMLElement>(".hr")!,
      dn = wk.querySelector<HTMLElement>(".dn")!;
    return { x0: hr.offsetWidth, cw: c.offsetWidth, ch: c.offsetHeight, y0: dn.offsetHeight };
  };
  const place = (e: HTMLElement, d: number, h: number, len: number) => {
    const g = G!;
    e.style.left = g.x0 + d * g.cw + 3 + "px";
    e.style.top = g.y0 + h * g.ch + 3 + "px";
    e.style.width = g.cw - 6 + "px";
    e.style.height = len * g.ch - 6 + "px";
  };

  /* --- the line --- */
  const clk = $("clk"), day = $("day"), who = $("who"), wtx = $("wtx"), dot = $("dot"), cap = $("cap"), ghost = $("ghost"), line = $("line");
  const wvb = [...$("wv").children] as HTMLElement[];
  const typed = (txt: string, p: number) => {
    const a = [...txt],
      n = Math.round(a.length * clamp(p));
    return esc(a.slice(0, n).join("")) + (p < 1 ? "<i></i>" : "") + `<span class="rest">${esc(a.slice(n).join(""))}</span>`;
  };

  const slot1 = $("slot1"), ph = $("ph1"), h1a = $("h1a"), h1b = $("h1b"), ph1a = $("ph1a"), ph1b = $("ph1b"), ph1c = $("ph1c");
  const sign = $("sign"), ans = $("ans"), save = $("save"), saved = $("saved"), nx = $("nx"), nx1 = $("nx1"), nx2 = $("nx2"), ny = $("ny");
  const pu1 = $("pu1"), pu2 = $("pu2"), mob = $("mob"), mob1 = $("mob1"), mob2 = $("mob2"), mobbt = $("mobbt"), yes = $("yes");
  const txl = [...document.querySelectorAll<HTMLElement>(".lp .tx .ln")], bk5 = $("bk5"), txtag = $("txtag"), mvtag = $("mvtag");
  const its = [...document.querySelectorAll<HTMLElement>(".lp .it")], pvs = [...document.querySelectorAll<HTMLElement>(".lp .pv")];
  const ptr = $("ptr"), home = document.querySelector<HTMLElement>(".lp .home")!;
  const o1 = $("o1"), o2 = $("o2"), fw = $("fw"), nm = $("nm"), cN = $("cN"), v0 = $("v0"), gr = $("gr"), sp7 = $("sp7");
  const vbars = [...v0.querySelectorAll<HTMLElement>(".bars b")];
  const wall = $("wall"), sunEl = $("sun"), lamp = $("lamp"), desk = $("desk"), nxtag = $("nxtag"), nxt = $("nxt");
  const ANS = "Yes, for groups up to six, on Saturdays.";

  function render(T: number, now: number) {
    const vh = innerHeight,
      y = scrollY;
    const P = [0, 0];
    for (let k = 2; k <= 9; k++) {
      const s = S[k],
        r = s.offsetHeight - vh;
      P[k] = reduce ? 1 : r > 0 ? clamp((y - s.offsetTop) / r) : y + vh * 0.5 >= s.offsetTop ? 1 : 0;
    }
    let own = 1;
    for (let k = 2; k <= 9; k++) if (y + vh * 0.5 >= S[k].offsetTop) own = k;
    if (reduce) T = 99;
    const top = (k: number) => S[k].offsetTop;

    /* room light: morning sun -> gold -> lamp */
    const dusk = seg(y, vh * 0.15, top(2)),
      late = seg(y, top(5), top(9));
    const ST: [number, string][] = [
      [0, "#F5EFE2"], [top(2) * 0.5, "#F0E3C4"], [top(2) - vh * 0.12, "#34486C"], [top(3), "#2C4064"], [top(4), "#25385B"],
      [top(5), "#1F3152"], [top(6), "#1B2C4A"], [top(7), "#15233D"], [top(8), "#0F1A2B"], [top(9), "#0B1422"],
    ];
    let wc = ST[ST.length - 1][1];
    for (let i = 1; i < ST.length; i++)
      if (y <= ST[i][0]) {
        wc = mixc(ST[i - 1][1], ST[i][1], seg(y, ST[i - 1][0], ST[i][0]));
        break;
      }
    wall.style.background = wc;
    sunEl.style.opacity = String(1 - seg(dusk, 0.45, 0.85));
    sunEl.style.transform = `translateX(${-dusk * 30}vw)`;
    sunEl.style.filter = `sepia(${seg(dusk, 0.1, 0.5)}) saturate(${1 + 3 * seg(dusk, 0.1, 0.5)}) hue-rotate(-12deg)`;
    lamp.style.opacity = String(seg(dusk, 0.55, 1) * lerp(0.55, 1, seg(y, top(2), top(8))));
    desk.style.opacity = String(Math.min(seg(y, top(2) - vh * 0.6, top(2) - vh * 0.2), 1 - seg(y, top(8) - vh * 0.9, top(8) - vh * 0.5)));
    desk.style.backgroundColor = mixc("#E6DCC6", mixc("#1F3152", "#0B1422", late), dusk);
    desk.style.borderTopColor = mixc("#D8CCB2", mixc("#34486C", "#1B2C4A", late), dusk);
    desk.style.filter = `brightness(${lerp(1, 0.9, dusk)})`;
    root!.classList.toggle("night", dusk > 0.55);

    /* the line's script: [from, label, text, typeFrom, typeTo, kind] */
    const L = { c: "10:40 am", d: "Monday", w: "Line open", t: "", tp: 1, k: "", talk: false };
    const run = (p: number, list: Step[]) => {
      for (const [f, w, t, a, b, k] of list)
        if (p >= f) {
          L.w = w;
          L.t = t;
          L.tp = a != null && b != null ? seg(p, a, b) : 1;
          L.k = k || "";
          L.talk = (k === "nova" || k === "caller") && L.tp < 1;
        }
    };

    /* 1: morning, the phone rings mid-color */
    {
      const ring = T >= 0.6 && T < 2.2,
        ans1 = T >= 2.2;
      ph.style.transform = ring && !reduce ? `translateX(${Math.sin(T * 60) * 3}px)` : "";
      [h1a, h1b].forEach((h, i) => {
        const u = ring ? (T * 1.2 + i * 0.5) % 1 : 0;
        h.style.opacity = String(ring ? (1 - u) * 0.9 : 0);
        h.style.transform = `scale(${1 + u * 1.6})`;
      });
      ph1a.textContent = ans1 ? "Nova answered" : ring ? "Incoming call" : "Salon line";
      ph1b.textContent = ans1 ? "0:" + String(Math.min(59, Math.floor(Math.max(0, T - 2.2) * 2.4 + 1))).padStart(2, "0") : "10:40";
      ph1c.innerHTML = ans1 ? "<b>On the call</b>You keep working." : ring ? "<b>Ringing</b>Your hands are in color." : "<b>&nbsp;</b>&nbsp;";
      const fl = seg(T, 6.7, 7.5),
        land = T >= 7.5;
      slot1.style.opacity = land ? "1" : "0";
      slot1.style.transform = land ? `scale(${1 + 0.06 * Math.sin(Math.PI * seg(T, 7.5, 7.8))})` : "";
      if (own === 1 && fl > 0 && fl < 1) {
        const lr = line.getBoundingClientRect(),
          sr = slot1.getBoundingClientRect(),
          g = ease(fl);
        ghost.style.display = "flex";
        ghost.className = "ghost slot";
        ghost.style.opacity = "1";
        ghost.innerHTML = slot1.innerHTML;
        ghost.style.width = sr.width + "px";
        const x = lerp(lr.left + lr.width / 2 - sr.width / 2, sr.left, g),
          yy = lerp(lr.bottom, sr.top, g) - Math.sin(Math.PI * g) * 60;
        ghost.style.transform = `translate(${x}px,${yy}px)`;
      } else ghost.style.display = "none";
      if (own === 1) {
        L.c = T >= 5 ? "10:41 am" : "10:40 am";
        run(T, [
          [0, "Line open", "Your hands are in color."],
          [0.6, "Ringing", "Salon line", null, null, "ring"],
          [2.2, "Nova", "Hi, I'm Nova, the salon's AI assistant. How can I help?", 2.2, 3.4, "nova"],
          [3.6, "Caller", "Do you have anything Saturday morning for a haircut?", 3.6, 4.7, "caller"],
          [4.9, "Nova", "Sam has 10:00 or 11:30. Which works for you?", 4.9, 5.8, "nova"],
          [6.0, "Caller", "10, please.", 6.0, 6.3, "caller"],
          [6.5, "Nova", "Booked. Saturday at 10:00 with Sam.", 6.5, 7.3, "nova"],
        ]);
      }
    }

    /* 2: 7 pm, closed; Priya moves to Thursday */
    {
      const p = P[2],
        f = ease(seg(p, 0.02, 0.14));
      sign.style.transform = `rotateY(${f * 180}deg)`;
      if (!G) G = gridGeo();
      evs.forEach(([e, d, h, len]) => place(e, d, h, len));
      place(was, 1, 1, 1.5);
      const m = ease(seg(p, 0.6, 0.78));
      place(pr, lerp(1, 3, m), lerp(1, 4, m), 1.5);
      pr.style.transform = `translateY(${-Math.sin(Math.PI * m) * 14}px)`;
      pr.style.boxShadow = m > 0 && m < 1 ? "inset 3px 0 0 #F0C34E,0 16px 26px -12px rgba(0,0,0,.4)" : "";
      was.style.opacity = m > 0 ? "1" : "0";
      pr.innerHTML = m >= 1 ? 'Priya, color<br><span class="tg ok" style="margin-top:4px;background:#fff">Moved</span>' : "Priya, color";
      mvtag.textContent = m >= 1 ? "Moved" : "Calendar";
      mvtag.className = m >= 1 ? "tg ok" : "tg";
      if (own === 2) {
        L.c = "7:02 pm";
        run(p, [
          [0, "Closed for the day", "The shop is quiet."],
          [0.1, "Ringing", "Salon line", null, null, "ring"],
          [0.16, "Nova", "Hi, I'm Nova. How can I help?", 0.16, 0.22, "nova"],
          [0.25, "Priya", "Hi, it's Priya. Can I move my color from Tuesday?", 0.25, 0.37, "caller"],
          [0.4, "Nova", "Thursday at 2:00 is open. Does that work?", 0.4, 0.5, "nova"],
          [0.53, "Priya", "Perfect.", 0.53, 0.57, "caller"],
          [0.8, "Nova", "Moved. Thursday at 2:00. See you then.", 0.8, 0.9, "nova"],
        ]);
      }
    }

    /* 3: needs you, answered once */
    {
      const p = P[3],
        c = eo(seg(p, 0.28, 0.36));
      ny.style.opacity = String(c);
      ny.style.transform = `translateY(${(1 - c) * 24}px)`;
      const tp = seg(p, 0.4, 0.56);
      ans.innerHTML = tp > 0 ? typed(ANS, tp) : '<span class="mute">Type the answer once.</span>';
      ans.className = "box" + (tp > 0 && tp < 1 ? " f" : "");
      save.style.transform = `scale(${1 - 0.08 * Math.sin(Math.PI * seg(p, 0.58, 0.62))})`;
      const sv = p >= 0.62;
      saved.style.opacity = sv ? "1" : "0";
      save.style.opacity = sv ? ".35" : "1";
      nx.classList.toggle("wait", p < 0.7);
      nxtag.textContent = p >= 0.7 ? "Answered" : "Next call";
      nxt.textContent = p >= 0.7 ? "8:05 pm" : "Later tonight";
      nx1.style.opacity = String(seg(p, 0.72, 0.76));
      nx2.style.opacity = String(seg(p, 0.8, 0.84));
      if (own === 3) {
        L.c = p >= 0.66 ? "8:05 pm" : "7:31 pm";
        run(p, [
          [0, "Line open", "Nova is answering."],
          [0.03, "Ringing", "Salon line", null, null, "ring"],
          [0.07, "Caller", "Do you do bridal parties?", 0.07, 0.13, "caller"],
          [0.15, "Nova", "I'll check with the owner. Can I take your name and number?", 0.15, 0.27, "nova"],
          [0.32, "Needs you", "1 question is waiting for you.", null, null, "wait"],
          [0.66, "Ringing", "Salon line", null, null, "ring"],
          [0.7, "Caller", "Do you do bridal parties?", 0.7, 0.76, "caller"],
          [0.78, "Nova", "We do, for groups up to six, on Saturdays.", 0.78, 0.9, "nova"],
        ]);
      }
    }

    /* 4: hand-off to your mobile */
    {
      const p = P[4],
        a = seg(p, 0.34, 0.46),
        b = seg(p, 0.46, 0.56);
      pu1.style.opacity = a > 0 && a < 1 ? "1" : "0";
      pu1.style.left = `calc(${a * 100}% - 6px)`;
      pu2.style.opacity = b > 0 && b < 1 ? "1" : "0";
      pu2.style.left = `calc(${b * 100}% - 6px)`;
      const rg = p >= 0.56 && p < 0.68,
        ok = p >= 0.68;
      mob1.textContent = ok ? "On the call" : rg ? "Salon line, from Nova" : "Your mobile";
      mob2.textContent = ok ? "0:0" + Math.min(9, 1 + Math.floor(seg(p, 0.68, 0.95) * 9)) : rg ? "Incoming call" : "7:48";
      mobbt.style.opacity = rg || ok ? "1" : "0";
      yes.style.transform = `scale(${1 + 0.12 * Math.sin(Math.PI * seg(p, 0.64, 0.68))})`;
      (mobbt.firstElementChild as HTMLElement).style.opacity = ok ? "0" : "1";
      mob.parentElement!.style.transform = rg && !reduce ? `translateX(${Math.sin(p * 900) * 2.5}px)` : "";
      mob.style.background = ok ? "linear-gradient(170deg,#24365a,#0F1A2B)" : "";
      if (own === 4) {
        L.c = "7:48 pm";
        run(p, [
          [0, "Line open", "Nova is answering."],
          [0.03, "Ringing", "Salon line", null, null, "ring"],
          [0.07, "Caller", "Can I talk to someone, please?", 0.07, 0.16, "caller"],
          [0.19, "Nova", "Of course. I'm passing you to the owner now.", 0.19, 0.31, "nova"],
          [0.34, "Handing off", "To your mobile", null, null, "ring"],
          [0.69, "With you", "Nova handed the call to you."],
        ]);
      }
    }

    /* 5: spanish */
    {
      const p = P[5],
        at = [0.1, 0.24, 0.4, 0.54, 0.66];
      txl.forEach((l, i) => {
        const u = eo(seg(p, at[i], at[i] + 0.05));
        l.style.opacity = String(u);
        l.style.transform = `translateY(${(1 - u) * 8}px)`;
      });
      const b = eo(seg(p, 0.72, 0.8));
      bk5.style.opacity = String(b);
      bk5.style.transform = `translateY(${(1 - b) * 16}px)`;
      txtag.textContent = p >= 0.72 ? "Booked" : "On the call";
      txtag.className = p >= 0.72 ? "tg ok" : "tg";
      if (own === 5) {
        L.c = "8:10 pm";
        run(p, [
          [0, "Line open", "Nova is answering."],
          [0.03, "Ringing", "Salon line", null, null, "ring"],
          [0.06, "Nova", "Hi, I'm Nova. How can I help?", 0.06, 0.12, "nova"],
          [0.16, "Caller", "Hola, ¿tienen cita para uñas el viernes?", 0.16, 0.26, "caller"],
          [0.32, "Nova", "Sí. El viernes a las 3:00 con Ana. ¿Le va bien?", 0.32, 0.42, "nova"],
          [0.48, "Caller", "Perfecto, gracias.", 0.48, 0.54, "caller"],
          [0.6, "Nova", "Listo. Viernes a las 3:00 con Ana.", 0.6, 0.7, "nova"],
        ]);
      }
    }

    /* 6: the home, row by row */
    {
      const p = P[6],
        sel = p < 0.38 ? 4 : p < 0.7 ? 3 : 2;
      its.forEach((e) => e.classList.toggle("sel", +e.dataset.i! === sel));
      pvs.forEach((v) => v.classList.toggle("on", +v.dataset.v! === sel));
      const hr = home.getBoundingClientRect(),
        pt = (i: number) => {
          const r = its[i].getBoundingClientRect();
          return [r.left - hr.left + 250, r.top - hr.top + r.height * 0.55];
        };
      const A = pt(4),
        B = pt(3),
        C = pt(2);
      const mv = (u: number[], v: number[], t: number) => u.map((x, i) => lerp(x, v[i], ease(t)));
      const xy = p < 0.3 ? A : p < 0.38 ? mv(A, B, seg(p, 0.3, 0.38)) : p < 0.62 ? B : p < 0.7 ? mv(B, C, seg(p, 0.62, 0.7)) : C;
      ptr.style.transform = `translate(${xy[0]}px,${xy[1]}px)`;
      if (own === 6) {
        L.c = "9:30 pm";
        run(p, [[0, "Line open", "Nova is answering tonight."]]);
      }
    }

    /* 7: setup */
    {
      const p = P[7];
      o1.classList.toggle("on", p >= 0.1);
      o2.classList.toggle("on", p >= 0.2);
      fw.style.opacity = String(seg(p, 0.26, 0.32));
      gr.style.opacity = String(0.4 + 0.6 * seg(p, 0.46, 0.5));
      sp7.classList.toggle("on", p >= 0.84);
      const tp = seg(p, 0.36, 0.46);
      nm.innerHTML = tp > 0 ? typed("Nova", tp) : '<span class="mute" style="font-weight:400;font-size:16px">Name it anything</span>';
      cN.classList.toggle("on", tp >= 1);
      const pl = p >= 0.54 && p < 0.8;
      v0.classList.toggle("on", p >= 0.54);
      vbars.forEach((b, j) => (b.style.height = (pl && !reduce ? 4 + 14 * Math.abs(Math.sin(p * 120 + j * 1.1)) : 3) + "px"));
      if (own === 7) {
        L.c = "10:05 pm";
        run(p, [
          [0, "Setup", "Your number, a name, a voice."],
          [0.54, "Marin, a sample voice", "Hi, I'm Nova. How can I help?", 0.54, 0.7, "nova"],
          [0.84, "Setup", "Nova is ready to answer."],
        ]);
      }
    }

    /* 8, 9: still */
    if (own >= 8) Object.assign(L, { c: "10:30 pm", w: "Line open", t: "Nova will pick up.", tp: 1, k: "" });

    /* paint the line */
    clk.textContent = L.c;
    day.textContent = L.d;
    wtx.textContent = L.w;
    who.className = "who" + (L.k === "nova" ? " nova" : "");
    dot.className = "dot" + (L.k === "ring" ? " ring" : L.k === "wait" ? " wait" : "");
    cap.innerHTML = L.t ? typed(L.t, L.tp) : "&nbsp;";
    wvb.forEach((b, j) => (b.style.height = (L.talk && !reduce ? 4 + 14 * Math.abs(Math.sin(now * 9 + j * 1.3)) : 4) + "px"));
  }

  /* driving: a scripted scroll for ?t=, one held moment for ?at=, else live */
  const DUR = [0, 8.6, 4.2, 4.6, 3.8, 3.6, 3.4, 3.6, 1.6, 2.2],
    GAP = 0.5;
  const timeline = (t: number) => {
    const vh = innerHeight;
    let acc = DUR[1];
    if (t < acc) return { y: 0, T: t };
    let prevEnd = 0;
    for (let k = 2; k <= 9; k++) {
      const s = S[k],
        tp = Math.min(s.offsetTop, document.documentElement.scrollHeight - vh),
        r = Math.max(0, s.offsetHeight - vh);
      if (t < acc + GAP) return { y: lerp(prevEnd, tp, ease(seg(t, acc, acc + GAP))), T: 99 };
      acc += GAP;
      if (t < acc + DUR[k] || k === 9) return { y: tp + seg(t, acc, acc + DUR[k]) * r, T: 99 };
      acc += DUR[k];
      prevEnd = tp + r;
    }
    return { y: 0, T: 99 };
  };
  const onResize = () => (G = null);
  addEventListener("resize", onResize);
  let raf = 0;
  const go = () => {
    G = null;
    if (q.has("at")) {
      const [k, p] = (q.get("at") ?? "").split(",").map(Number);
      const s = S[k];
      scrollTo(0, k === 1 ? 0 : s.offsetTop + (p || 0) * Math.max(0, s.offsetHeight - innerHeight));
      render(k === 1 ? p || 99 : 99, (p || 0) * 10);
    } else if (q.has("t")) {
      const t = parseFloat(q.get("t") ?? "") || 0;
      const s = timeline(t);
      scrollTo(0, s.y);
      render(s.T, t);
    } else {
      const t0 = performance.now();
      const loop = (n: number) => {
        const s = (n - t0) / 1000;
        render(s, s);
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
    removeEventListener("resize", onResize);
    root.classList.remove("js", "night");
    [...wk.querySelectorAll(".ev")].forEach((e) => e.remove());
  };
}

const esc = (s: string) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;");
