"use client";

import { useEffect, useRef, useState } from "react";

type Node = {
  id: string;
  name: string;
  role: string;
  x: number;
  y: number;
  big?: boolean;
  status: "delegating" | "working" | "idle";
  load: string;
};

const NODES: Node[] = [
  { id: "F",  name: "Friday", role: "Orchestrator",          x: 50, y: 50, big: true, status: "delegating", load: "11 in flight" },
  { id: "L",  name: "Lens",   role: "Research",              x: 14, y: 22, status: "working", load: "2 open" },
  { id: "St", name: "Stitch", role: "Sourcing & Outreach",   x: 86, y: 22, status: "working", load: "8 open" },
  { id: "O",  name: "Ora",    role: "Schedule",              x: 94, y: 60, status: "working", load: "1 open" },
  { id: "T",  name: "Tally",  role: "Finance",               x: 70, y: 86, status: "working", load: "3 open" },
  { id: "E",  name: "Echo",   role: "Recap",                 x: 28, y: 86, status: "idle",    load: "idle"   },
];

const EDGES: [string, string][] = [
  ["F", "L"], ["F", "St"], ["F", "O"], ["F", "T"], ["F", "E"],
  ["L", "St"],
  ["St", "O"],
  ["T", "E"],
];

const BY_ID: Record<string, Node> = Object.fromEntries(NODES.map((n) => [n.id, n]));

export function Team() {
  const [tick, setTick] = useState(0);

  useEffect(() => {
    if (typeof window !== "undefined" && window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    const id = window.setInterval(() => setTick((x) => x + 1), 950);
    return () => window.clearInterval(id);
  }, []);

  const active = new Set<number>([tick % EDGES.length, (tick + 3) % EDGES.length]);

  return (
    <section className="ld-team" id="team">
      <div className="ld-section">
        <div className="ld-kicker" data-ld-reveal>
          your org chart
        </div>
        <h2 className="ld-h2" data-ld-reveal>
          One brief. <em>The whole company moves.</em>
        </h2>
        <p className="ld-lede" data-ld-reveal>
          Every pod is a company, with a real <strong>Teams Inbox</strong>.
          Hand off a goal and <strong>Friday</strong>, your AI chief of staff,
          staffs it — specialists pick up the work, report back in threads you
          can read end-to-end, and nothing risky ships without your sign-off.
        </p>

        <div className="ld-team-stage" data-ld-reveal>
          <span className="ld-frame-label">paperclip · team view</span>
          <span className="ld-frame-meta">
            <span className="ld-dot ld-dot--live" /> Friday is delegating
          </span>

          <div className="ld-const">
            <svg viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true">
              {EDGES.map(([a, b], i) => {
                const A = BY_ID[a];
                const B = BY_ID[b];
                return (
                  <line
                    key={i}
                    x1={A.x}
                    y1={A.y}
                    x2={B.x}
                    y2={B.y}
                    className={active.has(i) ? "active" : ""}
                    vectorEffect="non-scaling-stroke"
                  />
                );
              })}
            </svg>

            {Array.from(active).map((i) => {
              const [a, b] = EDGES[i];
              const A = BY_ID[a];
              const B = BY_ID[b];
              return <Pulse key={`${tick}-${i}`} ax={A.x} ay={A.y} bx={B.x} by={B.y} />;
            })}

            {NODES.map((n) => (
              <div
                key={n.id}
                className="ld-const-node"
                style={{ left: `${n.x}%`, top: `${n.y}%` }}
              >
                <span className={"ld-puck" + (n.big ? " ld-puck--lead" : "")}>
                  {n.id === "F" ? <span>F</span> : n.id}
                </span>
                <span className="ld-puck-name">{n.name}</span>
                <span className="ld-puck-status">
                  <span className={"ld-dot " + (n.status === "idle" ? "ld-dot--idle" : "ld-dot--live")} />
                  <span>{n.load}</span>
                </span>
              </div>
            ))}
          </div>
        </div>

        <InboxMock />

        <div className="ld-inbox-captions" data-ld-reveal-group>
          <span>issues &amp; assignment</span>
          <span>auditable threads</span>
          <span>you approve the risky steps</span>
        </div>
      </div>
    </section>
  );
}

const ISSUES = [
  {
    id: "ISO-214",
    title: "Source 40 sales leads",
    who: "St",
    name: "Stitch",
    status: "in progress",
    kind: "live" as const,
    active: true,
  },
  {
    id: "ISO-213",
    title: "Weekly status report",
    who: "F",
    name: "Friday",
    status: "done",
    kind: "done" as const,
  },
  {
    id: "ISO-209",
    title: "Reconcile Q2 invoices",
    who: "T",
    name: "Tally",
    status: "needs approval",
    kind: "warn" as const,
  },
];

/**
 * Static mock of the Teams Inbox (the Paperclip-backed /teams surface) —
 * beat two of the team story: the constellation shows who's in the room,
 * this shows what running them actually feels like. Decorative; the
 * captions below carry the message for assistive tech.
 */
function InboxMock() {
  return (
    <div className="ld-inbox" data-ld-reveal aria-hidden="true">
      <span className="ld-frame-label">paperclip · inbox</span>
      <span className="ld-frame-meta">
        <span className="ld-dot ld-dot--live" /> 3 open issues
      </span>

      <div className="ld-inbox-body">
        <div className="ld-ib-list">
          {ISSUES.map((it) => (
            <div
              key={it.id}
              className={"ld-ib-item" + (it.active ? " is-active" : "")}
            >
              <span className="ld-ib-id">{it.id}</span>
              <span className="ld-ib-title">{it.title}</span>
              <span className="ld-ib-meta">
                <span className="ld-ib-avatar">{it.who}</span>
                <span className={`ld-chip-status ld-chip-status--${it.kind}`}>
                  {it.status}
                </span>
              </span>
            </div>
          ))}
        </div>

        <div className="ld-ib-thread">
          <div className="ld-ib-thread-hd">
            <span className="ld-ib-id">ISO-214</span>
            <span className="ld-ib-thread-title">Source 40 sales leads</span>
            <span className="ld-chip-status ld-chip-status--live">
              in progress
            </span>
          </div>

          <div className="ld-ib-msg">
            <span className="ld-ib-avatar ld-ib-avatar--you">You</span>
            <div>
              <span className="ld-ib-author">You</span>
              <p>
                Find 40 leads for the Q3 outreach push — mid-market SaaS,
                US/EU.
              </p>
            </div>
          </div>

          <div className="ld-ib-msg">
            <span className="ld-ib-avatar ld-ib-avatar--lead">F</span>
            <div>
              <span className="ld-ib-author">
                Friday <i>· orchestrator</i>
              </span>
              <p>
                On it. Splitting across Stitch (sourcing) and Lens
                (research). ETA tonight.
              </p>
              <span className="ld-ib-tag">assigned → Stitch, Lens</span>
            </div>
          </div>

          <div className="ld-ib-msg">
            <span className="ld-ib-avatar">St</span>
            <div>
              <span className="ld-ib-author">
                Stitch <i>· sourcing</i>
              </span>
              <p>38 verified so far — list attached.</p>
              <span className="ld-ib-attach">⎘ leads-q3.csv</span>
            </div>
          </div>

          <div className="ld-approval">
            <div>
              <span className="ld-approval-kicker">approval needed</span>
              <p>Stitch wants to send 42 intro emails via Gmail.</p>
            </div>
            <div className="ld-approval-actions">
              <span className="ld-approve-btn">Approve</span>
              <span className="ld-approve-btn ld-approve-btn--ghost">
                Hold
              </span>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

function Pulse({ ax, ay, bx, by }: { ax: number; ay: number; bx: number; by: number }) {
  const [t, setT] = useState(0);
  const rafRef = useRef<number | null>(null);
  const startRef = useRef<number | null>(null);

  useEffect(() => {
    const step = (ts: number) => {
      if (startRef.current === null) startRef.current = ts;
      const p = Math.min(1, (ts - startRef.current) / 850);
      setT(p);
      if (p < 1) rafRef.current = requestAnimationFrame(step);
    };
    rafRef.current = requestAnimationFrame(step);
    return () => {
      if (rafRef.current !== null) cancelAnimationFrame(rafRef.current);
    };
  }, []);

  const x = ax + (bx - ax) * t;
  const y = ay + (by - ay) * t;

  return (
    <span
      className="ld-const-pulse"
      style={{
        left: `${x}%`,
        top: `${y}%`,
        transform: `translate(-50%, -50%) scale(${1 - t * 0.4})`,
        opacity: 1 - t * 0.7,
      }}
      aria-hidden="true"
    />
  );
}
