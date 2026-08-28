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
        borderColor: "var(--border)",
        fontSize: Math.round(size * 0.4),
      }}
      aria-hidden
    >
      {agent.monogram}
    </span>
  );
}

export function AgentPill({
  children,
  tone = "neutral",
}: {
  children: ReactNode;
  tone?: "neutral" | "midnight" | "brass" | "err" | "warn";
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
