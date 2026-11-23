"use client";

import { useEffect, useRef } from "react";

/*
 * The home hero: three product demos that play in turn, each opened by a short title card.
 *  - Lightbulb Phone: a call rings in, Ava answers, the salon calendar fills, a text confirms.
 *  - Spark: the real Spark launch clip (/landing/demo/spark-demo.mp4) on a laptop.
 *  - Lightbulb Meetings: a Meet call where Lightbulb answers out loud and keeps the notes.
 * The scenes are plain markup; one effect drives them imperatively (the same posture as
 * components/landing/SparkStage.tsx), so a 60fps caption typewriter never re-renders React. Each demo resets
 * behind the title card, then waits on the card before it plays. Reduced motion: no autoplay
 * video, near-instant steps. Portraits are Unsplash photos, self-hosted under /landing/demo.
 */

const D = "/landing/demo";
const PEOPLE = { jess: `${D}/jess.jpg`, maya: `${D}/maya.jpg`, daniel: `${D}/daniel.jpg`, sam: `${D}/sam.jpg` };

const INTROS = [
  { title: "Lightbulb Phone", line: "Your business line, answered. Watch Ava pick up a call and book the appointment.", tag: "" },
  { title: "Spark", line: "Your Mac, by voice. Hold fn, say what you want done, and watch it happen.", tag: "" },
  { title: "Lightbulb Meetings", line: "A teammate in your Google Meet. It answers when you ask and keeps the notes.", tag: "Coming soon" },
];

const TABS = [
  { name: "Lightbulb Phone", what: "Answers your business line" },
  { name: "Spark", what: "Runs your Mac by voice" },
  { name: "Lightbulb Meetings", what: "Sits in your Google Meet" },
];

const PhoneIcon = () => (
  <svg width="22" height="22" viewBox="0 0 24 24" fill="#fff" aria-hidden="true">
    <path d="M6.6 10.8a15.1 15.1 0 0 0 6.6 6.6l2.2-2.2c.3-.3.7-.4 1-.2 1.1.4 2.3.6 3.6.6.6 0 1 .4 1 1V20c0 .6-.4 1-1 1A17 17 0 0 1 3 4c0-.6.4-1 1-1h3.5c.6 0 1 .4 1 1 0 1.3.2 2.5.6 3.6.1.3 0 .7-.2 1z" />
  </svg>
);
const Level = () => (
  <span className="hd-lvl" aria-hidden="true">
    <i />
    <i />
    <i />
  </span>
);

export function HomeDemos() {
  const root = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const el = root.current;
    if (!el) return;
    const $ = <T extends HTMLElement = HTMLElement>(sel: string) => el.querySelector(sel) as T;
    const all = <T extends HTMLElement = HTMLElement>(sel: string) => [...el.querySelectorAll<T>(sel)];
    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const tabs = all<HTMLButtonElement>(".hd-tab");
    const scenes = all(".hd-scene");
    const video = $<HTMLVideoElement>(".hd-lcd video");

    let run = 0;
    let gate: Promise<void> = Promise.resolve();
    let raf = 0;
    const alive = (id: number) => id === run;
    const wait = (ms: number) => new Promise<void>((r) => setTimeout(r, reduce ? Math.min(ms, 300) : ms));
    const type = async (node: HTMLElement, text: string, id: number, speed = 28) => {
      if (reduce) return void (node.textContent = text);
      for (let c = 1; c <= text.length; c++) {
        if (!alive(id)) return;
        node.textContent = text.slice(0, c);
        await wait(speed);
      }
    };

    async function phone(id: number) {
      const sc = scenes[0];
      const state = $(".hd-p-state"), name = $(".hd-p-name"), sub = $(".hd-p-sub"), ring = $(".hd-p-rings");
      const orb = $(".hd-p-orb"), box = $(".hd-p-caps"), slot = $(".hd-p-slot"), sms = $(".hd-p-sms"), burst = $(".hd-burst");
      sc.classList.remove("live", "ended");
      state.textContent = "Incoming call";
      name.textContent = "Jess Rivera";
      sub.textContent = "mobile (917) 555-0148";
      box.innerHTML = "";
      sms.classList.remove("on");
      burst.classList.remove("go");
      slot.className = "hd-p-slot";
      slot.innerHTML = "<span>Open</span>";
      ring.classList.add("on");
      await gate;
      if (!alive(id)) return;
      const talk = async (who: string, text: string, ava = false) => {
        const p = document.createElement("p");
        if (ava) p.className = "ava";
        p.innerHTML = `<small>${who}</small><span></span>`;
        box.appendChild(p);
        while (box.children.length > 2) box.firstChild?.remove();
        if (ava) orb.classList.add("talk");
        await type(p.querySelector("span")!, text, id, ava ? 24 : 28);
        orb.classList.remove("talk");
        await wait(450);
      };
      await wait(1700);
      if (!alive(id)) return;
      ring.classList.remove("on");
      sc.classList.add("live");
      state.textContent = "Answered by Ava";
      name.textContent = "Ava";
      sub.textContent = "Bella's Salon assistant";
      await wait(400);
      await talk("Ava", "Hi, thanks for calling Bella's! How can I help?", true);
      if (!alive(id)) return;
      await talk("Jess", "Can I get a cut and color with Maria Saturday morning?");
      if (!alive(id)) return;
      slot.className = "hd-p-slot hold";
      slot.innerHTML = "Holding for Jess<small>Checking Maria's book</small>";
      await talk("Ava", "Maria has 11:30 on Saturday. Shall I book it?", true);
      if (!alive(id)) return;
      await talk("Jess", "Yes, perfect.");
      if (!alive(id)) return;
      slot.className = "hd-p-slot done";
      slot.innerHTML = "Jess Rivera<small>Cut and color, booked by Ava</small>";
      const r = slot.getBoundingClientRect();
      const c = (slot.offsetParent as HTMLElement).getBoundingClientRect();
      burst.style.left = `${r.left - c.left + r.width / 2}px`;
      burst.style.top = `${r.top - c.top + r.height / 2}px`;
      void burst.offsetWidth;
      burst.classList.add("go");
      await wait(800);
      if (!alive(id)) return;
      sms.classList.add("on");
      await wait(1200);
      if (!alive(id)) return;
      sc.classList.add("ended");
      state.textContent = "Call ended";
      sub.textContent = "1:12, appointment booked";
      await wait(3000);
    }

    async function spark(id: number) {
      video.pause();
      video.currentTime = 0;
      await gate;
      if (!alive(id)) return;
      if (reduce) return wait(4000);
      try {
        await video.play();
      } catch {
        /* autoplay refused: the poster frame stays, and the timer below moves on */
      }
      await new Promise<void>((resolve) => {
        const done = () => {
          video.removeEventListener("ended", done);
          clearTimeout(t);
          resolve();
        };
        const t = setTimeout(done, 16500);
        video.addEventListener("ended", done);
      });
      await wait(500);
    }

    async function meet(id: number) {
      const cc = $(".hd-m-cc"), ccav = $(".hd-m-ccav"), ccwho = $(".hd-m-ccwho"), cctext = $(".hd-m-cctext");
      const feed = $(".hd-m-feed"), toast = $(".hd-m-toast");
      const tiles = all(".hd-m-tile");
      const lbOrb = tiles[3].querySelector(".hd-orb")!;
      const faces = [PEOPLE.maya, PEOPLE.daniel, PEOPLE.sam, ""];
      feed.innerHTML = "";
      cc.classList.remove("on");
      toast.classList.remove("on");
      tiles.forEach((t) => t.classList.remove("talk"));
      await gate;
      if (!alive(id)) return;
      const say = async (n: number, who: string, text: string) => {
        tiles.forEach((t) => t.classList.remove("talk"));
        tiles[n].classList.add("talk");
        if (n === 3) lbOrb.classList.add("talk");
        ccav.className = "hd-m-ccav" + (n === 3 ? " hd-orb" : "");
        ccav.style.backgroundImage = faces[n] ? `url(${faces[n]})` : "";
        ccwho.textContent = who;
        cctext.textContent = "";
        cc.classList.add("on");
        await type(cctext, text, id, n === 3 ? 24 : 28);
        await wait(550);
        tiles[n].classList.remove("talk");
        lbOrb.classList.remove("talk");
      };
      const card = (kind: string, chip: string, text: string, face = "") => {
        const d = document.createElement("div");
        d.className = `hd-card ${kind}`;
        d.innerHTML = `<span class="hd-chip">${chip}</span><span>${text}</span>` + (face ? `<img src="${face}" alt="" />` : "");
        feed.appendChild(d);
        while (feed.children.length > 5) feed.firstChild?.remove();
      };
      await wait(400);
      if (!alive(id)) return;
      toast.classList.add("on");
      card("say", "Joined", "Taking notes for Launch sync. Agenda: launch date, press note.");
      await wait(1400);
      toast.classList.remove("on");
      if (!alive(id)) return;
      await say(0, "Maya", "Lightbulb, what did we land on for the launch date?");
      if (!alive(id)) return;
      await say(3, "Lightbulb", "December 4th. Daniel moved it on Tuesday so support gets a week to prepare.");
      if (!alive(id)) return;
      card("dec", "Decision", "Launch moves to December 4");
      await wait(500);
      if (!alive(id)) return;
      await say(1, "Daniel", "Right. Can you send Sam the latest press note?");
      if (!alive(id)) return;
      await say(3, "Lightbulb", "Sent press-note-v3 to Sam in Slack. I'll track the review.");
      if (!alive(id)) return;
      card("act", "Done", "Sent press-note-v3.pdf to Sam in Slack");
      await wait(450);
      card("fu", "Follow-up", "Sam reviews the press note by Friday", PEOPLE.sam);
      await wait(500);
      if (!alive(id)) return;
      await say(2, "Sam", "Got it, thanks. I'll have notes by Thursday.");
      if (!alive(id)) return;
      cc.classList.remove("on");
      await wait(2600);
    }

    const demos = [phone, spark, meet];
    const card = $(".hd-intro");

    async function intro(i: number, id: number) {
      $(".hd-intro-title").textContent = INTROS[i].title;
      $(".hd-intro-line").textContent = INTROS[i].line;
      $(".hd-intro-tag").textContent = INTROS[i].tag;
      card.classList.remove("on");
      void card.offsetWidth;
      card.classList.add("on");
      await wait(2600);
      if (alive(id)) card.classList.remove("on");
      await wait(350);
    }

    async function play(i: number) {
      const id = ++run;
      if (i !== 1) video.pause();
      scenes.forEach((s, k) => s.classList.toggle("on", k === i));
      tabs.forEach((t, k) => {
        t.setAttribute("aria-selected", String(k === i));
        t.querySelector("i")!.style.width = "0";
      });
      const bar = tabs[i].querySelector("i")!;
      const t0 = performance.now();
      cancelAnimationFrame(raf);
      const tick = () => {
        if (!alive(id)) return;
        bar.style.width = `${Math.min(100, (performance.now() - t0) / 170)}%`;
        raf = requestAnimationFrame(tick);
      };
      tick();
      let release = () => {};
      gate = new Promise<void>((r) => (release = r));
      const demo = demos[i](id);
      await intro(i, id);
      release();
      await demo;
      if (alive(id)) play((i + 1) % demos.length);
    }

    const onTab = (e: Event) => play(Number((e.currentTarget as HTMLElement).dataset.i));
    tabs.forEach((t) => t.addEventListener("click", onTab));
    // Each visit opens on a random product: no product is the lead.
    play(Math.floor(Math.random() * demos.length));

    return () => {
      run++;
      cancelAnimationFrame(raf);
      video.pause();
      tabs.forEach((t) => t.removeEventListener("click", onTab));
    };
  }, []);

  return (
    <div className="hd" ref={root}>
      <div className="hd-tabs" role="tablist" aria-label="Product demos">
        {TABS.map((t, i) => (
          <button key={t.name} type="button" role="tab" className="hd-tab" data-i={i} aria-selected="false">
            <b>{t.name}</b>
            <span>{t.what}</span>
            <i aria-hidden="true" />
          </button>
        ))}
      </div>

      <div className="hd-stage" aria-live="polite">
        {/* Lightbulb Phone */}
        <div className="hd-scene hd-phone" role="tabpanel" aria-label="Lightbulb Phone demo">
          <div className="hd-bg" />
          <div className="hd-p-wrap">
            <div className="hd-iphone">
              <div className="hd-island" />
              <div className="hd-p-screen">
                <div className="hd-p-top">
                  <small className="hd-p-state">Incoming call</small>
                  <b className="hd-p-name">Jess Rivera</b>
                  <span className="hd-p-sub">mobile (917) 555-0148</span>
                </div>
                <div className="hd-p-face">
                  <div className="hd-p-rings" aria-hidden="true">
                    <i />
                    <i />
                    <i />
                  </div>
                  {/* eslint-disable-next-line @next/next/no-img-element -- a small decorative avatar in an animated scene */}
                  <img className="hd-p-caller" src={PEOPLE.jess} alt="" />
                  <div className="hd-orb hd-p-orb" />
                  {/* eslint-disable-next-line @next/next/no-img-element */}
                  <img className="hd-p-mini" src={PEOPLE.jess} alt="" />
                </div>
                <div className="hd-p-caps" />
                <div className="hd-p-actions" aria-hidden="true">
                  <span className="dec">
                    <PhoneIcon />
                  </span>
                  <span className="acc">
                    <PhoneIcon />
                  </span>
                </div>
              </div>
            </div>
            <div className="hd-p-cal">
              <div className="hd-p-calhead">
                <div>
                  <b>Saturday</b>
                  <span>November 29</span>
                </div>
                <em>Bella&apos;s Salon</em>
              </div>
              <div className="hd-p-grid">
                <div />
                <div className="who">
                  <i className="c" />
                  Maria
                </div>
                <div className="who">
                  <i className="s" />
                  Jo
                </div>
                <div className="t">10:00</div>
                <div className="hd-ev c">
                  Ana P.<small>Color</small>
                </div>
                <div />
                <div className="t">11:30</div>
                <div className="hd-p-slot">
                  <span>Open</span>
                </div>
                <div className="hd-ev s">
                  Lee M.<small>Cut</small>
                </div>
                <div className="t">1:00</div>
                <div />
                <div className="hd-ev s">
                  Rosa K.<small>Blowout</small>
                </div>
                <div className="t">2:30</div>
                <div className="hd-ev c">
                  Tom W.<small>Cut</small>
                </div>
                <div className="hd-ev v">
                  Kai L.<small>Beard trim</small>
                </div>
              </div>
              <div className="hd-burst" aria-hidden="true">
                <i />
                <i />
                <i />
                <i />
                <i />
                <i />
                <i />
                <i />
              </div>
            </div>
          </div>
          <div className="hd-p-sms">
            <span className="app" aria-hidden="true">
              <svg width="18" height="18" viewBox="0 0 24 24" fill="#fff">
                <path d="M12 3C6.5 3 2 6.6 2 11c0 2.4 1.3 4.6 3.4 6.1L4.5 21l4.2-2.2c1 .3 2.1.4 3.3.4 5.5 0 10-3.6 10-8S17.5 3 12 3z" />
              </svg>
            </span>
            <div>
              <div className="row">
                <b>Bella&apos;s Salon</b>
                <small>now</small>
              </div>
              <p>You&apos;re booked Sat 11:30 with Maria for a cut and color. Reply C to change.</p>
            </div>
          </div>
        </div>

        {/* Spark */}
        <div className="hd-scene hd-spark" role="tabpanel" aria-label="Spark demo">
          <div className="hd-bg" />
          <div className="hd-laptop">
            <div className="hd-lid">
              <div className="hd-lcd">
                <video
                  src={`${D}/spark-demo.mp4`}
                  poster={`${D}/spark-poster.jpg`}
                  muted
                  playsInline
                  preload="metadata"
                  aria-label="Spark puts the PDFs on a Mac desktop into a Receipts folder, then sets a reminder from a circled invitation"
                />
              </div>
            </div>
            <div className="hd-base">
              <span />
            </div>
          </div>
        </div>

        {/* Lightbulb Meetings */}
        <div className="hd-scene hd-meet" role="tabpanel" aria-label="Lightbulb Meetings demo">
          <div className="hd-bg" />
          <div className="hd-m-wrap">
            <div className="hd-m-call">
              <div className="hd-m-head">
                <span className="rec" />
                Launch sync<em>10:02 AM</em>
              </div>
              <div className="hd-m-tiles">
                {(
                  [
                    ["Maya", PEOPLE.maya, "-2s"],
                    ["Daniel", PEOPLE.daniel, "-7s"],
                    ["Sam", PEOPLE.sam, "-4s"],
                  ] as const
                ).map(([who, src, delay]) => (
                  <div className="hd-m-tile" key={who}>
                    {/* eslint-disable-next-line @next/next/no-img-element */}
                    <img src={src} alt="" style={{ animationDelay: delay }} />
                    <Level />
                    <small>{who}</small>
                  </div>
                ))}
                <div className="hd-m-tile hd-m-lb">
                  <div className="hd-orb hd-m-orb" />
                  <Level />
                  <small>Lightbulb</small>
                </div>
              </div>
              <div className="hd-m-cc">
                <span className="hd-m-ccav" />
                <div>
                  <b className="hd-m-ccwho" />
                  <span className="hd-m-cctext" />
                </div>
              </div>
              <div className="hd-m-bar" aria-hidden="true">
                <span />
                <span />
                <span />
                <span className="end" />
              </div>
              <div className="hd-m-toast">
                <span className="hd-orb" />
                Lightbulb joined the call
              </div>
            </div>
            <div className="hd-m-side">
              <div className="hd-m-sidehead">
                <span className="hd-orb" />
                <div>
                  <b>Lightbulb</b>
                  <small>Notes and actions, live</small>
                </div>
              </div>
              <div className="hd-m-feed" />
            </div>
          </div>
        </div>

        <div className="hd-intro" aria-hidden="true">
          <div>
            <span className="hd-intro-tag" />
            <h3 className="hd-intro-title" />
            <p className="hd-intro-line" />
          </div>
        </div>
      </div>
    </div>
  );
}
