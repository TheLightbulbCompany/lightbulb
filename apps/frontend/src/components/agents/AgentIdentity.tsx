"use client";

import Link from "next/link";
import { Check, Plus, Star } from "lucide-react";

import type { EditorialAgent } from "@/lib/agentDesign";

export function Monogram({
  agent,
  size = 48,
  radius = 12,
}: {
  agent: Pick<EditorialAgent, "monogram" | "accent" | "tint">;
  size?: number;
  radius?: number;
}) {
  return (
    <span
      className="agent-monogram"
      style={{
        width: size,
        height: size,
        borderRadius: radius,
        color: agent.accent,
        backgroundColor: agent.tint,
        borderColor: `${agent.accent}22`,
        fontSize: Math.round(size * 0.4),
      }}
      aria-hidden
    >
      {agent.monogram}
    </span>
  );
}

export function StatusDot({ status = "active" }: { status?: string }) {
  const normalized = status.toLowerCase();
  const className =
    normalized === "thinking" || normalized === "running"
      ? "status-dot status-dot--thinking"
      : normalized === "paused" || normalized === "idle"
        ? "status-dot status-dot--paused"
        : normalized === "error"
          ? "status-dot status-dot--error"
          : "status-dot status-dot--active";
  return <span className={className} aria-label={normalized} />;
}

export function AgentPill({ children, tone = "neutral" }: { children: React.ReactNode; tone?: "neutral" | "forest" | "danger" | "warn" }) {
  return <span className={`agent-pill agent-pill--${tone}`}>{children}</span>;
}

export function SectionLabel({ label, count }: { label: string; count?: string | number }) {
  return (
    <div className="section-label">
      <span>{label}</span>
      {count !== undefined && <b>{count}</b>}
    </div>
  );
}

export function AgentCard({
  agent,
  installed,
  installing,
  onInstall,
  href,
}: {
  agent: EditorialAgent;
  installed?: boolean;
  installing?: boolean;
  onInstall?: (slug: string) => void;
  href?: string;
}) {
  const body = (
    <article className="agent-card">
      <div className="agent-card__head">
        <Monogram agent={agent} size={52} />
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <h3>{agent.name}</h3>
            {agent.new && <AgentPill tone="forest">New</AgentPill>}
          </div>
          <p>{agent.role}</p>
        </div>
      </div>
      <p className="agent-card__quote">&ldquo;{agent.vibe}&rdquo;</p>
      <div className="agent-card__pills">
        {agent.channels.slice(0, 3).map((channel) => (
          <AgentPill key={channel}>{channel}</AgentPill>
        ))}
      </div>
      <div className="agent-card__foot">
        <span>
          <Star size={12} fill="currentColor" /> {agent.rating} · {agent.installs}
        </span>
        {onInstall && (
          <button
            type="button"
            className={`editorial-btn editorial-btn--sm ${installed ? "editorial-btn--secondary" : "editorial-btn--primary"}`}
            disabled={installed || installing}
            onClick={(event) => {
              event.preventDefault();
              event.stopPropagation();
              onInstall(agent.slug);
            }}
          >
            {installed ? <Check size={13} /> : <Plus size={13} />}
            {installed ? "Installed" : installing ? "Installing" : "Install"}
          </button>
        )}
      </div>
    </article>
  );

  if (!href) return body;
  return (
    <Link href={href} className="block no-underline">
      {body}
    </Link>
  );
}
