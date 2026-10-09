"use client";
import type { RoomSubagents, SubagentStatus } from "@lightbulb/shared";
import { subagentActivity } from "@lightbulb/shared";

const ICON: Record<SubagentStatus | "dispatched", { glyph: string; label: string; cls: string }> = {
  running: { glyph: "", label: "Running", cls: "is-running" },
  queued: { glyph: "○", label: "Queued", cls: "is-queued" },
  completed: { glyph: "✓", label: "Completed", cls: "is-ok" },
  failed: { glyph: "✗", label: "Failed", cls: "is-error" },
  interrupted: { glyph: "■", label: "Stopped", cls: "is-warn" },
  dispatched: { glyph: "•", label: "Dispatched", cls: "is-muted" },
};

export function delegationIdOf(result: unknown): string | null {
  const parse = (v: unknown): unknown => { if (typeof v !== "string") return v; try { return JSON.parse(v); } catch { return null; } };
  const r = parse(result) as { delegation_id?: unknown } | null;
  return r && typeof r.delegation_id === "string" ? r.delegation_id : null;
}

/** Upstream delegate.tsx: one row per child of this fan-out. A child nothing is watching shows a
 *  static dot and never spins. */
export function DelegateCard({ state, agentId, delegationId, agentLive }: {
  state: RoomSubagents; agentId: string; delegationId: string; agentLive: boolean;
}) {
  const children = state.items.filter((i) => i.agentId === agentId && i.delegationId === delegationId)
    .sort((a, b) => a.taskIndex - b.taskIndex);
  if (!children.length) {
    return agentLive ? null : <div className="room-delegate-card"><p className="room-delegate-row">
      <span className={`room-delegate-icon ${ICON.dispatched.cls}`} aria-label={ICON.dispatched.label}>{ICON.dispatched.glyph}</span>
      <span className="room-delegate-goal">Background subagents dispatched</span></p></div>;
  }
  return <div className="room-delegate-card">{children.map((c) => {
    const icon = ICON[c.status];
    const meta = [c.model, c.durationSeconds != null ? `${Math.round(c.durationSeconds)}s` : null].filter(Boolean).join(" · ");
    return <div key={c.id} className="room-delegate-row">
      <span className={`room-delegate-icon ${icon.cls}`} aria-label={icon.label}>{icon.glyph}</span>
      <span className="room-delegate-goal">{c.goal}</span>
      {meta && <span className="room-delegate-meta">{meta}</span>}
      <span className="room-delegate-activity">{subagentActivity(c)}</span>
    </div>;
  })}</div>;
}
