"use client";

import { useEffect, useRef } from "react";
import gsap from "gsap";
import { ScrollTrigger } from "gsap/ScrollTrigger";

gsap.registerPlugin(ScrollTrigger);

const STEPS = [
  {
    num: "01",
    title: "Brief your agent",
    desc: "Talk to your pod like a teammate — here, or from Slack, WhatsApp, Telegram, Discord. One message is the whole assignment.",
  },
  {
    num: "02",
    title: "It reaches your tools",
    desc: "Your agent connects natively to Gmail, Office 365, Salesforce and thirty more — all from inside your own container.",
  },
  {
    num: "03",
    title: "Work comes back done",
    desc: "Multi-step jobs run in the background and land finished: drafted in your tone, sent to the right people, filed where it belongs.",
  },
];

function ChatVisual() {
  return (
    <div className="ld-card">
      <div className="ld-card-hd">
        <span className="ld-card-dot" />
        <span>Friday</span>
        <span className="ld-card-time">pod · live</span>
      </div>
      <div className="ld-card-bd">
        <div className="ld-bubble ld-bubble--user">
          Draft my weekly status report
        </div>
        <div className="ld-bubble ld-bubble--agent">
          On it — pulling your calendar, email threads and last week&apos;s
          notes to write it in your style.
          <span className="ld-type-cursor" aria-hidden="true" />
        </div>
      </div>
    </div>
  );
}

function HubVisual() {
  return (
    <div className="ld-hub">
      <div className="ld-hub-core">
        <div className="ld-hub-ring" />
        <span>8</span>
      </div>
      <svg
        className="ld-hub-lines"
        width="240"
        height="30"
        viewBox="0 0 240 30"
        fill="none"
        aria-hidden="true"
      >
        <line x1="120" y1="0" x2="28" y2="30" />
        <line x1="120" y1="0" x2="120" y2="30" />
        <line x1="120" y1="0" x2="212" y2="30" />
      </svg>
      <div className="ld-hub-row">
        <div className="ld-int">
          <div className="ld-int-bubble">
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img
              src="/logos/gmail.png"
              width="26"
              height="26"
              alt="Gmail"
              style={{ objectFit: "contain" }}
            />
          </div>
          <span>Gmail</span>
        </div>
        <div className="ld-int">
          <div className="ld-int-bubble">
            <svg width="22" height="22" viewBox="0 0 21 21" xmlns="http://www.w3.org/2000/svg">
              <rect x="0" y="0" width="10" height="10" fill="#F25022" />
              <rect x="11" y="0" width="10" height="10" fill="#7FBA00" />
              <rect x="0" y="11" width="10" height="10" fill="#00A4EF" />
              <rect x="11" y="11" width="10" height="10" fill="#FFB900" />
            </svg>
          </div>
          <span>Office 365</span>
        </div>
        <div className="ld-int">
          <div className="ld-int-bubble">
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img
              src="/logos/salesforce.png"
              width="38"
              height="38"
              alt="Salesforce"
              style={{ objectFit: "contain" }}
            />
          </div>
          <span>Salesforce</span>
        </div>
      </div>
    </div>
  );
}

function TasksVisual() {
  const tasks = [
    { text: "Report drafted in your tone & style", meta: "09:41" },
    { text: "Email sent to your team via Gmail", meta: "09:42" },
    { text: "Document saved to OneDrive", meta: "09:42" },
  ];
  return (
    <div className="ld-tasks">
      {tasks.map((t) => (
        <div className="ld-task" key={t.text}>
          <div className="ld-task-check">
            <svg
              width="10"
              height="10"
              viewBox="0 0 10 10"
              fill="none"
              stroke="white"
              strokeWidth="1.8"
              strokeLinecap="round"
              strokeLinejoin="round"
            >
              <path d="M1.5 5l2.5 2.5 4.5-4" />
            </svg>
          </div>
          <span>{t.text}</span>
          <span className="ld-task-meta">{t.meta}</span>
        </div>
      ))}
    </div>
  );
}

const VISUALS = [ChatVisual, HubVisual, TasksVisual];

/**
 * Walkthrough with fully native scrolling — no pinning, no scroll-jacking.
 * On desktop the visual column is position:sticky; as each step in the
 * left rail crosses mid-viewport its panel crossfades in. On mobile (and
 * under reduced motion) it's a plain stacked list, each step carrying its
 * own inline visual.
 */
export function HowItWorks() {
  const rootRef = useRef<HTMLElement>(null);

  useEffect(() => {
    const root = rootRef.current;
    if (!root) return;

    const mm = gsap.matchMedia();
    mm.add(
      "(min-width: 769px) and (prefers-reduced-motion: no-preference)",
      () => {
        const panels = gsap.utils.toArray<HTMLElement>(".ld-how-panel", root);
        const steps = gsap.utils.toArray<HTMLElement>(".ld-how-step", root);
        const bars = gsap.utils.toArray<HTMLElement>(".ld-how-bar", root);
        const rail = root.querySelector(".ld-how-rail");

        rail?.classList.add("is-animated");
        gsap.set(panels.slice(1), { autoAlpha: 0 });

        let active = -1;
        const setActive = (idx: number) => {
          if (idx === active) return;
          const prev = active;
          active = idx;
          steps.forEach((s, i) => s.classList.toggle("is-active", i === idx));
          if (prev >= 0) {
            gsap.to(panels[prev], {
              autoAlpha: 0,
              y: -16,
              duration: 0.28,
              ease: "power2.out",
              overwrite: "auto",
            });
          }
          gsap.fromTo(
            panels[idx],
            { y: prev >= 0 ? 22 : 0 },
            { autoAlpha: 1, y: 0, duration: 0.45, ease: "power3.out", overwrite: "auto" }
          );
          gsap.fromTo(
            bars[idx],
            { scaleX: 0 },
            { scaleX: 1, duration: 0.6, ease: "power2.out", overwrite: "auto" }
          );
        };
        setActive(0);

        const triggers = steps.map((step, i) =>
          ScrollTrigger.create({
            trigger: step,
            start: "top 60%",
            end: "bottom 60%",
            onEnter: () => setActive(i),
            onEnterBack: () => setActive(i),
          })
        );

        return () => {
          triggers.forEach((t) => t.kill());
          steps.forEach((s) => s.classList.remove("is-active"));
          rail?.classList.remove("is-animated");
        };
      }
    );

    return () => mm.revert();
  }, []);

  return (
    <section className="ld-how" id="how" ref={rootRef}>
      <div className="ld-how-frame">
        <div className="ld-kicker" data-ld-reveal>
          how it works
          <span className="ld-kicker-index">02 — 08</span>
        </div>
        <h2 className="ld-h2" data-ld-reveal>
          One brief in. <em>Finished work out.</em>
        </h2>

        <div className="ld-how-grid">
          <div className="ld-how-rail">
            {STEPS.map((step, i) => {
              const Visual = VISUALS[i];
              return (
                <div className="ld-how-step" key={step.num}>
                  <span className="ld-how-bar" aria-hidden="true" />
                  <span className="ld-how-num">{step.num}</span>
                  <h3>{step.title}</h3>
                  <p>{step.desc}</p>
                  <div className="ld-how-mobile-visual">
                    <Visual />
                  </div>
                </div>
              );
            })}
          </div>

          <div className="ld-how-stage" aria-hidden="true">
            <div className="ld-how-panel">
              <ChatVisual />
            </div>
            <div className="ld-how-panel">
              <HubVisual />
            </div>
            <div className="ld-how-panel">
              <TasksVisual />
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}
