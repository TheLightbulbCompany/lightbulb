"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { capture } from "@/lib/analytics";

type Arch = "aarch64" | "x64";

// Browsers on macOS don't expose CPU architecture directly. Chromium-based
// browsers offer it through navigator.userAgentData high-entropy hints
// ("arm" or "x86"); Safari/Firefox don't support UA-CH on macOS at all.
// We default to Apple Silicon (supermajority since 2020) and surface an
// "Intel Mac?" fallback link so the minority can self-select.
async function detectArch(): Promise<Arch> {
  const uaData = (
    navigator as Navigator & {
      userAgentData?: {
        getHighEntropyValues?: (
          hints: string[],
        ) => Promise<{ architecture?: string }>;
      };
    }
  ).userAgentData;
  if (!uaData?.getHighEntropyValues) return "aarch64";
  try {
    const v = await uaData.getHighEntropyValues(["architecture"]);
    return v.architecture === "x86" ? "x64" : "aarch64";
  } catch {
    return "aarch64";
  }
}

export function Hero() {
  const [arch, setArch] = useState<Arch>("aarch64");

  useEffect(() => {
    let cancelled = false;
    detectArch().then((a) => {
      if (!cancelled) setArch(a);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    const seq = document.getElementById("wfSeq");
    if (!seq || window.matchMedia("(prefers-reduced-motion: reduce)").matches)
      return;

    const AGENT_TEXT =
      "On it — I'll pull your calendar, emails, and last week's notes to write it in your style.";
    const typeTarget = document.getElementById("wfTypeTarget");
    const agentBubble = document.getElementById("wfAgentBubble");
    let typeTimer: ReturnType<typeof setInterval> | null = null;
    let timeouts: ReturnType<typeof setTimeout>[] = [];

    function on(el: Element | null) {
      if (el) el.setAttribute("data-wf-on", "");
    }
    function off(el: Element) {
      el.removeAttribute("data-wf-on");
    }

    function $$(sel: string) {
      return Array.from(seq!.querySelectorAll(sel));
    }
    function $(sel: string) {
      return seq!.querySelector(sel);
    }

    function typeText(
      text: string,
      target: HTMLElement | null,
      speed: number,
      done: () => void
    ) {
      if (!target) return;
      let i = 0;
      target.textContent = "";
      typeTimer = setInterval(() => {
        if (i < text.length) {
          target.textContent += text[i];
          i++;
        } else {
          clearInterval(typeTimer!);
          typeTimer = null;
          done();
        }
      }, speed);
    }

    function resetAll() {
      seq!.classList.remove("wf-out");
      $$("[data-wf-on]").forEach(off);
      $$(".wf-done").forEach((el) => el.classList.remove("wf-done"));
      if (typeTarget) typeTarget.textContent = "";
      if (typeTimer) {
        clearInterval(typeTimer);
        typeTimer = null;
      }
    }

    function play() {
      resetAll();
      timeouts = [];

      const stepHds = $$(".wf-step-hd");
      const chatCard = $(".wf-chat-card");
      const userBub = $(".wf-bubble-u");
      const connLines = $$(".wf-conn-line");
      const connBadges = $$(".wf-conn-badge");
      const conns = $$(".wf-conn");
      const hub = $(".wf-hub");
      const agentNode = $(".wf-agent-node");
      const svgLines = $("svg");
      const ints = $$(".wf-int");
      const tasksCard = $(".wf-tasks-card");
      const tasks = $$(".wf-task");

      const t = (ms: number, fn: () => void) =>
        timeouts.push(setTimeout(fn, ms));

      // Phase 1: Chat
      t(200, () => on(stepHds[0]));
      t(500, () => on(chatCard));
      t(800, () => on(userBub));
      t(1400, () => {
        on(agentBubble);
        typeText(AGENT_TEXT, typeTarget, 25, () => {});
      });

      // Phase 2: Connectors + Integrations
      t(4000, () => {
        on(conns[0]);
        on(connLines[0]);
      });
      t(4200, () => on(connLines[1]));
      t(4400, () => on(connBadges[0]));
      t(4800, () => on(stepHds[1]));
      t(5100, () => {
        on(hub);
        on(agentNode);
      });
      t(5500, () => on(svgLines));
      t(5700, () => on(ints[0]));
      t(5900, () => on(ints[1]));
      t(6100, () => on(ints[2]));

      // Phase 3: Connector 2 + Results
      t(6800, () => {
        on(conns[1]);
        on(connLines[2]);
      });
      t(7000, () => on(connLines[3]));
      t(7200, () => on(connBadges[1]));
      t(7500, () => on(stepHds[2]));
      t(7800, () => on(tasksCard));
      t(8100, () => on(tasks[0]));
      t(8500, () => on(tasks[1]));
      t(8900, () => on(tasks[2]));
      t(9300, () => tasksCard?.classList.add("wf-done"));

      // Phase 4: Hold, then fade out and restart
      t(11500, () => seq!.classList.add("wf-out"));
      t(12300, () => play());
    }

    // Start the sequence when hero is visible
    const heroObs = new IntersectionObserver(
      (entries) => {
        entries.forEach((e) => {
          if (e.isIntersecting) {
            play();
            heroObs.disconnect();
          }
        });
      },
      { threshold: 0.3 }
    );
    heroObs.observe(seq);

    return () => {
      heroObs.disconnect();
      timeouts.forEach(clearTimeout);
      if (typeTimer) clearInterval(typeTimer);
    };
  }, []);

  return (
    <section className="landing-hero">
      <div className="hero-left">
        <p className="eyebrow">isol8 — automated workflows</p>
        <h1 className="hero-h1">
          Deploy an agent
          <br />
          <em>in one click.</em>
        </h1>
        <p className="hero-sub">
          Pre-built agents for every branch of your business. Pick one, click
          deploy — it connects to the tools you already use and runs the
          workflow autonomously.
        </p>
        <div className="hero-ctas">
          <Link href="/chat" className="btn-large" onClick={() => capture("landing_cta_clicked")}>
            Start your pod
          </Link>
          <Link href="#features" className="btn-secondary">
            Learn more
          </Link>
        </div>
        <p className="hero-fine">
          Free to start · No credit card required ·{" "}
          <a
            href={`/api/download/desktop?arch=${arch}`}
            className="hero-download-link"
            onClick={() => capture("landing_download_clicked", { arch })}
          >
            Download for Mac
          </a>
          {arch === "aarch64" && (
            <>
              {" · "}
              <a
                href="/api/download/desktop?arch=x64"
                className="hero-download-link"
                onClick={() =>
                  capture("landing_download_clicked", { arch: "x64" })
                }
              >
                Intel Mac?
              </a>
            </>
          )}
        </p>
      </div>

      <div className="hero-right wf-seq" id="wfSeq">
        {/* Step 1: Chat */}
        <div className="wf-step-hd">
          <span className="wf-num">01</span>
          <span className="wf-step-title">Chat with your agent</span>
        </div>
        <div className="wf-chat-card">
          <div className="wf-chat-hd">
            <div className="wf-chat-dot" />
            <span className="wf-chat-name">Your Agent</span>
          </div>
          <div className="wf-chat-bd">
            <div className="wf-bubble wf-bubble-u">
              Draft my weekly status report
            </div>
            <div className="wf-bubble wf-bubble-a" id="wfAgentBubble">
              <span id="wfTypeTarget" />
              <span className="wf-type-cursor" />
            </div>
          </div>
        </div>

        {/* Connector 1→2 */}
        <div className="wf-conn">
          <div className="wf-conn-line" />
          <div className="wf-conn-badge">connects to your tools</div>
          <div className="wf-conn-line" />
        </div>

        {/* Step 2: Integrations */}
        <div className="wf-step-hd">
          <span className="wf-num">02</span>
          <span className="wf-step-title">Reaches your workflow</span>
        </div>
        <div className="wf-hub">
          <div className="wf-agent-node">
            <div className="wf-agent-ring" />
            <span
              style={{
                fontFamily: "var(--font-lora)",
                fontStyle: "italic",
                fontWeight: 400,
                fontSize: "32px",
                color: "#1a1a1a",
                lineHeight: 1,
                userSelect: "none",
              }}
            >
              8
            </span>
          </div>
          <svg
            width="220"
            height="24"
            viewBox="0 0 220 24"
            fill="none"
          >
            <line x1="110" y1="0" x2="23" y2="24" stroke="#c5d8cd" strokeWidth="1.5" strokeDasharray="3 3" />
            <line x1="110" y1="0" x2="110" y2="24" stroke="#c5d8cd" strokeWidth="1.5" strokeDasharray="3 3" />
            <line x1="110" y1="0" x2="197" y2="24" stroke="#c5d8cd" strokeWidth="1.5" strokeDasharray="3 3" />
          </svg>
          <div className="wf-int-row">
            {/* Gmail */}
            <div className="wf-int">
              <div className="wf-int-bubble">
                <svg width="24" height="24" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
                  <path d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92c-.26 1.37-1.04 2.53-2.21 3.31v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.09z" fill="#4285F4" />
                  <path d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z" fill="#34A853" />
                  <path d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l3.66-2.84z" fill="#FBBC05" />
                  <path d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z" fill="#EA4335" />
                </svg>
              </div>
              <span className="wf-int-lbl">Gmail</span>
            </div>
            {/* Microsoft */}
            <div className="wf-int">
              <div className="wf-int-bubble">
                <svg width="22" height="22" viewBox="0 0 21 21" xmlns="http://www.w3.org/2000/svg">
                  <rect x="0" y="0" width="10" height="10" fill="#F25022" />
                  <rect x="11" y="0" width="10" height="10" fill="#7FBA00" />
                  <rect x="0" y="11" width="10" height="10" fill="#00A4EF" />
                  <rect x="11" y="11" width="10" height="10" fill="#FFB900" />
                </svg>
              </div>
              <span className="wf-int-lbl">Office 365</span>
            </div>
            {/* Salesforce */}
            <div className="wf-int">
              <div className="wf-int-bubble">
                <img src="/logos/salesforce.png" width="38" height="38" alt="Salesforce" style={{ objectFit: "contain" }} />
              </div>
              <span className="wf-int-lbl">Salesforce</span>
            </div>
          </div>
        </div>

        {/* Connector 2→3 */}
        <div className="wf-conn">
          <div className="wf-conn-line" />
          <div className="wf-conn-badge">executes tasks</div>
          <div className="wf-conn-line" />
        </div>

        {/* Step 3: Results */}
        <div className="wf-step-hd">
          <span className="wf-num">03</span>
          <span className="wf-step-title">Gets things done</span>
        </div>
        <div className="wf-tasks-card">
          {[
            "Report drafted in your tone & style",
            "Email sent to your team via Gmail",
            "Document saved to OneDrive automatically",
          ].map((text) => (
            <div className="wf-task" key={text}>
              <div className="wf-check">
                <svg width="10" height="10" viewBox="0 0 10 10" fill="none" stroke="white" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M1.5 5l2.5 2.5 4.5-4" />
                </svg>
              </div>
              <span className="wf-task-text">{text}</span>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}
