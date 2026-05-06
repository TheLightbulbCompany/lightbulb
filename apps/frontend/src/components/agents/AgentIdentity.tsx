"use client";

import type { ReactNode } from "react";

export interface MonogramAgent {
  monogram: string;
  accent: string;
  tint: string;
}

export function Monogram({
  agent,
  size = 48,
  radius = 12,
}: {
  agent: MonogramAgent;
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

export function AgentPill({
  children,
  tone = "neutral",
}: {
  children: ReactNode;
  tone?: "neutral" | "forest" | "danger" | "warn";
}) {
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
