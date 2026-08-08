"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";

import {
  landingMarketplaceAgents,
  marketplaceListingHref,
  type LandingMarketplaceAgent,
} from "@/lib/marketplace/landingAgents";

type Node = {
  id: "F" | LandingMarketplaceAgent["id"];
  name: string;
  role: string;
  x: number;
  y: number;
  big?: boolean;
  status: "delegating" | "working" | "idle";
  load: string;
  agent?: LandingMarketplaceAgent;
};

const AGENT_BY_ID = Object.fromEntries(
  landingMarketplaceAgents.map((agent) => [agent.id, agent]),
) as Record<LandingMarketplaceAgent["id"], LandingMarketplaceAgent>;

const NODES: Node[] = [
  { id: "F", name: "Friday", role: "Orchestrator", x: 50, y: 50, big: true, status: "delegating", load: "organizing" },
  { id: "L", name: "Lens", role: "Research", x: 14, y: 22, status: "working", load: "research", agent: AGENT_BY_ID.L },
  { id: "St", name: "Stitch", role: "Sales", x: 86, y: 22, status: "working", load: "sales", agent: AGENT_BY_ID.St },
  { id: "O", name: "Ora", role: "Schedule", x: 94, y: 60, status: "working", load: "calendar", agent: AGENT_BY_ID.O },
  { id: "T", name: "Tally", role: "Finance", x: 70, y: 86, status: "working", load: "finance", agent: AGENT_BY_ID.T },
  { id: "E", name: "Echo", role: "Recap", x: 28, y: 86, status: "idle", load: "recaps", agent: AGENT_BY_ID.E },
];

const EDGES: [Node["id"], Node["id"]][] = [
  ["F", "L"], ["F", "St"], ["F", "O"], ["F", "T"], ["F", "E"],
  ["L", "St"],
  ["St", "O"],
  ["T", "E"],
];

const BY_ID: Record<Node["id"], Node> = Object.fromEntries(NODES.map((n) => [n.id, n])) as Record<Node["id"], Node>;

export function Team() {
  const [tick, setTick] = useState(0);
  const [activeAgentId, setActiveAgentId] = useState<LandingMarketplaceAgent["id"] | null>(null);
  const stageRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (typeof window !== "undefined" && window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    const id = window.setInterval(() => setTick((x) => x + 1), 950);
    return () => window.clearInterval(id);
  }, []);

  useEffect(() => {
    if (!activeAgentId) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setActiveAgentId(null);
    };
    const onPointerDown = (event: PointerEvent) => {
      if (!stageRef.current?.contains(event.target as globalThis.Node)) setActiveAgentId(null);
    };
    window.addEventListener("keydown", onKeyDown);
    window.addEventListener("pointerdown", onPointerDown);
    return () => {
      window.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("pointerdown", onPointerDown);
    };
  }, [activeAgentId]);

  const animatedActive = new Set<number>([tick % EDGES.length, (tick + 3) % EDGES.length]);
  const activeEdgeIndexes = activeAgentId
    ? new Set(EDGES.map(([a, b], i) => ((a === "F" && b === activeAgentId) || (b === "F" && a === activeAgentId)) ? i : -1).filter((i) => i >= 0))
    : animatedActive;
  const activeNode = activeAgentId ? BY_ID[activeAgentId] : null;
  const activeAgent = activeNode?.agent ?? null;

  return (
    <section className="ld-team" id="team">
      <div className="ld-section">
        <div className="ld-kicker" data-ld-reveal>
          marketplace to team
        </div>
        <h2 className="ld-h2" data-ld-reveal>
          Hire the agents you need. <em>Friday keeps them moving.</em>
        </h2>
        <p className="ld-lede" data-ld-reveal>
          Browse the marketplace, add specialists to your team, and let Friday
          coordinate the handoffs. You can inspect the work, approve important
          decisions, and swap in new help as the business changes.
        </p>

        <div className="ld-team-stage" data-ld-reveal ref={stageRef}>
          <span className="ld-frame-label">marketplace → your team</span>
          <span className="ld-frame-meta">
            <span className="ld-dot ld-dot--live" /> Friday connects the work
          </span>

          <div className={"ld-const" + (activeAgentId ? " has-active-agent" : "")}>
            <svg viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true">
              {EDGES.map(([a, b], i) => {
                const A = BY_ID[a];
                const B = BY_ID[b];
                const isActive = activeEdgeIndexes.has(i);
                const isDimmed = Boolean(activeAgentId && !isActive && a !== activeAgentId && b !== activeAgentId);
                return (
                  <line
                    key={i}
                    x1={A.x}
                    y1={A.y}
                    x2={B.x}
                    y2={B.y}
                    className={(isActive ? "active" : "") + (isDimmed ? " is-dimmed" : "")}
                    vectorEffect="non-scaling-stroke"
                  />
                );
              })}
            </svg>

            {Array.from(activeEdgeIndexes).map((i) => {
              const [a, b] = EDGES[i];
              const A = BY_ID[a];
              const B = BY_ID[b];
              return <Pulse key={activeAgentId ? "selected-" + i : tick + "-" + i} ax={A.x} ay={A.y} bx={B.x} by={B.y} />;
            })}

            {NODES.map((n) => {
              const isActive = activeAgentId === n.id;
              const isDimmed = Boolean(activeAgentId && n.id !== "F" && !isActive);
              const content = (
                <>
                  <span className={"ld-puck" + (n.big ? " ld-puck--lead" : "")}>{n.id === "F" ? <span>F</span> : n.id}</span>
                  <span className="ld-puck-name">{n.name}</span>
                  <span className="ld-puck-status">
                    <span className={"ld-dot " + (n.status === "idle" ? "ld-dot--idle" : "ld-dot--live")} />
                    <span>{n.load}</span>
                  </span>
                </>
              );

              return n.agent ? (
                <button
                  key={n.id}
                  type="button"
                  className={"ld-const-node ld-const-node--agent" + (isActive ? " is-active" : "") + (isDimmed ? " is-dimmed" : "")}
                  style={{ left: `${n.x}%`, top: `${n.y}%` }}
                  onPointerEnter={(event) => {
                    if (event.pointerType !== "touch") setActiveAgentId(n.agent!.id);
                  }}
                  onFocus={() => setActiveAgentId(n.agent!.id)}
                  onClick={() => setActiveAgentId((current) => current === n.agent!.id ? null : n.agent!.id)}
                  aria-expanded={isActive}
                  aria-describedby={isActive ? "ld-agent-card" : undefined}
                >
                  {content}
                </button>
              ) : (
                <div key={n.id} className="ld-const-node" style={{ left: `${n.x}%`, top: `${n.y}%` }}>
                  {content}
                </div>
              );
            })}

            {activeAgent && activeNode && (
              <div
                id="ld-agent-card"
                className={"ld-agent-card " + cardClass(activeNode)}
                role="dialog"
                aria-label={`${activeAgent.name} marketplace listing`}
              >
                <p className="ld-agent-eyebrow">Marketplace agent</p>
                <h3>{activeAgent.name} <span>— {activeAgent.role}</span></h3>
                <p>{activeAgent.value}</p>
                <Link href={marketplaceListingHref(activeAgent.listingId)}>
                  View in marketplace <span aria-hidden="true">→</span>
                </Link>
              </div>
            )}
          </div>
        </div>

        <InboxMock />

        <div className="ld-inbox-captions" data-ld-reveal-group>
          <span>hire from marketplace</span>
          <span>agents work together</span>
          <span>you approve key decisions</span>
        </div>
      </div>
    </section>
  );
}

function cardClass(node: Node) {
  const horizontal = node.x > 72 ? "ld-agent-card--left" : node.x < 28 ? "ld-agent-card--right" : "ld-agent-card--center";
  const vertical = node.y > 66 ? "ld-agent-card--above" : "ld-agent-card--below";
  return `${horizontal} ${vertical}`;
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
