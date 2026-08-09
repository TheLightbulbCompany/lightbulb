"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";

import {
  landingMarketplaceAgents,
  marketplaceListingHref,
  type LandingMarketplaceAgent,
} from "@/lib/marketplace/landingAgents";

type RosterAgentId =
  | "F"
  | LandingMarketplaceAgent["id"]
  | "Ops"
  | "Th"
  | "PA"
  | "A"
  | "P"
  | "V"
  | "J";

type Node = {
  id: RosterAgentId;
  name: string;
  role: string;
  x: number;
  y: number;
  big?: boolean;
  status: "delegating" | "working" | "idle" | "review" | "error";
  load: string;
  value: string;
  agent?: LandingMarketplaceAgent;
};

const AGENT_BY_ID = Object.fromEntries(
  landingMarketplaceAgents.map((agent) => [agent.id, agent]),
) as Record<LandingMarketplaceAgent["id"], LandingMarketplaceAgent>;

const NODES: Node[] = [
  {
    id: "F",
    name: "Friday",
    role: "CEO / orchestrator",
    x: 50,
    y: 50,
    big: true,
    status: "delegating",
    load: "directing",
    value: "Friday turns company goals into work, delegates to the roster, and brings humans in when judgement matters.",
  },
  { id: "Ops", name: "Platform Ops", role: "Reliability", x: 50, y: 8, status: "idle", load: "infra", value: "Keeps the platform healthy, watches reliability work, and turns production signals into follow-up tasks." },
  { id: "L", name: "Lens", role: "Research", x: 26, y: 14, status: "working", load: "research", value: AGENT_BY_ID.L.value, agent: AGENT_BY_ID.L },
  { id: "Th", name: "Thread", role: "Customer support", x: 74, y: 14, status: "idle", load: "support", value: "Turns customer conversations into clear threads, next actions, and owner-ready follow-up." },
  { id: "E", name: "Echo", role: "Recap", x: 10, y: 34, status: "idle", load: "recaps", value: AGENT_BY_ID.E.value, agent: AGENT_BY_ID.E },
  { id: "St", name: "Stitch", role: "Sales", x: 90, y: 34, status: "working", load: "sales", value: AGENT_BY_ID.St.value, agent: AGENT_BY_ID.St },
  { id: "V", name: "Vera", role: "Review", x: 14, y: 64, status: "review", load: "review", value: "Reviews work, catches risk, and helps keep public-facing changes clear before they ship." },
  { id: "PA", name: "Product Analytics", role: "Metrics", x: 86, y: 64, status: "idle", load: "scorecard", value: "Keeps product and company metrics readable so the team can see what is changing." },
  { id: "T", name: "Tally", role: "Finance", x: 28, y: 86, status: "working", load: "finance", value: AGENT_BY_ID.T.value, agent: AGENT_BY_ID.T },
  { id: "O", name: "Ora", role: "Schedule", x: 72, y: 86, status: "working", load: "calendar", value: AGENT_BY_ID.O.value, agent: AGENT_BY_ID.O },
  { id: "A", name: "Atlas", role: "Recruiter", x: 50, y: 94, status: "idle", load: "hiring", value: "Finds or designs the next specialist when Friday sees a gap in the company." },
  { id: "P", name: "Pulse", role: "Monitoring", x: 36, y: 38, status: "idle", load: "watching", value: "Watches for important changes and wakes the right person or agent when something needs attention." },
  { id: "J", name: "Jarvis", role: "Engineering", x: 64, y: 38, status: "working", load: "coding", value: "Builds and verifies product changes, then hands them back with evidence for review." },
];

const EDGES: [Node["id"], Node["id"]][] = [
  ["F", "Ops"], ["F", "L"], ["F", "Th"], ["F", "E"], ["F", "St"], ["F", "V"], ["F", "PA"], ["F", "T"], ["F", "O"], ["F", "A"], ["F", "P"], ["F", "J"],
  ["Ops", "Th"],
  ["Th", "St"],
  ["St", "PA"],
  ["PA", "O"],
  ["O", "A"],
  ["A", "T"],
  ["T", "V"],
  ["V", "E"],
  ["E", "L"],
  ["L", "Ops"],
  ["J", "St"],
  ["J", "L"],
  ["P", "V"],
  ["P", "T"],
];

const BY_ID: Record<Node["id"], Node> = Object.fromEntries(NODES.map((n) => [n.id, n])) as Record<Node["id"], Node>;

export function Team() {
  const [tick, setTick] = useState(0);
  const [activeNodeId, setActiveNodeId] = useState<Node["id"] | null>(null);
  const stageRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (typeof window !== "undefined" && window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    const id = window.setInterval(() => setTick((x) => x + 1), 950);
    return () => window.clearInterval(id);
  }, []);

  useEffect(() => {
    if (!activeNodeId) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setActiveNodeId(null);
    };
    const onPointerDown = (event: PointerEvent) => {
      if (!stageRef.current?.contains(event.target as globalThis.Node)) setActiveNodeId(null);
    };
    window.addEventListener("keydown", onKeyDown);
    window.addEventListener("pointerdown", onPointerDown);
    return () => {
      window.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("pointerdown", onPointerDown);
    };
  }, [activeNodeId]);

  const animatedActive = new Set<number>([tick % EDGES.length, (tick + 3) % EDGES.length]);
  const activeEdgeIndexes = activeNodeId
    ? new Set(EDGES.map(([a, b], i) => ((a === "F" && b === activeNodeId) || (b === "F" && a === activeNodeId) || a === activeNodeId || b === activeNodeId) ? i : -1).filter((i) => i >= 0))
    : animatedActive;
  const activeNode = activeNodeId ? BY_ID[activeNodeId] : null;
  const activeAgent = activeNode?.agent ?? null;

  return (
    <section className="ld-team" id="team">
      <div className="ld-section">
        <div className="ld-kicker" data-ld-reveal>
          live company roster
        </div>
        <h2 className="ld-h2" data-ld-reveal>
          Watch the company form around the work. <em>Friday keeps it moving.</em>
        </h2>
        <p className="ld-lede" data-ld-reveal>
          The spiderweb is the current Isol8 roster: Friday in the middle,
          marketplace specialists and company agents around her, each taking a
          real slice of the business. As the company grows, the roster becomes
          part of the story people can see.
        </p>

        <div className="ld-team-stage" data-ld-reveal ref={stageRef}>
          <span className="ld-frame-label">marketplace → your team</span>
          <span className="ld-frame-meta">
            <span className="ld-dot ld-dot--live" /> live Isol8 roster
          </span>

          <div className={"ld-const" + (activeNodeId ? " has-active-agent" : "")}>
            <svg viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true">
              {EDGES.map(([a, b], i) => {
                const A = BY_ID[a];
                const B = BY_ID[b];
                const isActive = activeEdgeIndexes.has(i);
                const isDimmed = Boolean(activeNodeId && !isActive && a !== activeNodeId && b !== activeNodeId);
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
              return <Pulse key={activeNodeId ? "selected-" + i : tick + "-" + i} ax={A.x} ay={A.y} bx={B.x} by={B.y} />;
            })}

            {NODES.map((n) => {
              const isActive = activeNodeId === n.id;
              const isDimmed = Boolean(activeNodeId && n.id !== "F" && !isActive);
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

              return !n.big ? (
                <button
                  key={n.id}
                  type="button"
                  className={"ld-const-node ld-const-node--agent" + (isActive ? " is-active" : "") + (isDimmed ? " is-dimmed" : "")}
                  style={{ left: `${n.x}%`, top: `${n.y}%` }}
                  onPointerEnter={(event) => {
                    if (event.pointerType !== "touch") setActiveNodeId(n.id);
                  }}
                  onFocus={() => setActiveNodeId(n.id)}
                  onClick={() => setActiveNodeId((current) => current === n.id ? null : n.id)}
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

            {activeNode && activeNode.id !== "F" && (
              <div
                id="ld-agent-card"
                className={"ld-agent-card " + cardClass(activeNode)}
                role="dialog"
                aria-label={`${activeNode.name} roster detail`}
              >
                <p className="ld-agent-eyebrow">{activeAgent ? "Marketplace agent" : "Isol8 roster"}</p>
                <h3>{activeNode.name} <span>— {activeAgent?.role ?? activeNode.role}</span></h3>
                <p>{activeAgent?.value ?? activeNode.value}</p>
                {activeAgent ? (
                  <Link href={marketplaceListingHref(activeAgent.listingId)}>
                    View in marketplace <span aria-hidden="true">→</span>
                  </Link>
                ) : (
                  <span className="ld-agent-roster-note">Live company agent</span>
                )}
              </div>
            )}
          </div>
        </div>

        <InboxMock />

        <div className="ld-inbox-captions" data-ld-reveal-group>
          <span>company building in public</span>
          <span>marketplace agents plus roster agents</span>
          <span>humans approve key decisions</span>
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
    title: "ISOA-77 landing review",
    who: "J",
    name: "Jarvis",
    status: "in review",
    kind: "warn" as const,
  },
  {
    id: "ISO-209",
    title: "Monitor production health",
    who: "Ops",
    name: "Platform Ops",
    status: "idle",
    kind: "done" as const,
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
      <span className="ld-frame-label">live work queue</span>
      <span className="ld-frame-meta">
        <span className="ld-dot ld-dot--live" /> work moving now
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
                Put the company roster directly under the hero so visitors can
                see Isol8 building in public.
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
                On it. Keeping the marketplace story, moving the live roster
                spiderweb up, and handing the PR back with evidence.
              </p>
              <span className="ld-ib-tag">assigned → Jarvis, Lens, Stitch</span>
            </div>
          </div>

          <div className="ld-ib-msg">
            <span className="ld-ib-avatar">St</span>
            <div>
              <span className="ld-ib-author">
                Jarvis <i>· engineering</i>
              </span>
              <p>PR updated — desktop and mobile screenshots ready for review.</p>
              <span className="ld-ib-attach">⎘ landing-roster.png</span>
            </div>
          </div>

          <div className="ld-approval">
            <div>
              <span className="ld-approval-kicker">review needed</span>
              <p>Owner reviews the PR before merge or deploy.</p>
            </div>
            <div className="ld-approval-actions">
              <span className="ld-approve-btn">Approve</span>
              <span className="ld-approve-btn ld-approve-btn--ghost">
                Request changes
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
