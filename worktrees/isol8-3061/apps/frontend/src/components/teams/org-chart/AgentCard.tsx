"use client";

// Ported + enriched from upstream Paperclip's OrgChart card render
// (paperclip/ui/src/pages/OrgChart.tsx) (MIT, (c) 2025 Paperclip AI).
//
// Upstream shows a glyph avatar + status dot, name, title (falling back to a
// role label), the adapter label, and capabilities (line-clamped). We mirror
// that in the editorial theme, minus the adapter label (the BFF strips
// `adapterType` from the agents list as a token-leak defense — see
// routers/teams/agents.py `_redact_agent`), and add a relative "last active"
// line plus the pause/error reason when the status carries one.
//
// No image avatar: upstream's `Agent.icon` is an opaque icon-picker name
// (rendered via `<AgentIcon>`), NOT a URL — there is no avatar-URL field on
// the agent wire shape. We render the Bot glyph for every card rather than
// plumb a never-populated `iconUrl` through an `<img>` that would never load.
//
// Click navigates to the agent detail. The `data-org-card` marker lets the
// pan/zoom viewport tell card clicks apart from drag-to-pan.

import Link from "next/link";
import { Bot } from "lucide-react";
import { cn } from "@/lib/utils";
import { handleShellLinkClick } from "@/lib/shellNav";
import { agentInspectorUrl } from "@/components/teams/shared/lib/agentInspectorUrl";
import { timeAgo } from "@/components/teams/shared/lib/timeAgo";
import { statusDotClass, statusDotColor } from "./statusColors";
import { CARD_W, CARD_H } from "./orgChartLayout";

export interface AgentCardProps {
  id: string;
  name: string;
  role: string;
  status: string;
  x: number;
  y: number;
  /** Short one-liner; falls back to the role when absent. */
  title?: string | null;
  /** Longer freeform description, line-clamped to two lines. */
  capabilities?: string | null;
  /** ISO timestamp → rendered as a relative "active <n> ago" line. */
  lastHeartbeatAt?: string | null;
  /** Shown beside the status when paused (upstream PauseReason code). */
  pauseReason?: string | null;
  /** Shown beside the status when errored. */
  errorReason?: string | null;
}

/** Humanize a status token for the secondary status line. */
function statusLabel(status: string): string {
  if (!status) return "Idle";
  return status.charAt(0).toUpperCase() + status.slice(1);
}

// Upstream PauseReason codes (packages/shared/src/constants.ts) → prose.
// Rendering the raw code ("Paused · company_archived") leaks an internal enum
// into the card; map it to a human label and fall back to the raw code for any
// future value.
const PAUSE_REASON_LABELS: Record<string, string> = {
  manual: "paused manually",
  budget: "budget exhausted",
  system: "paused by system",
  company_archived: "company archived",
};

function pauseReasonLabel(reason: string): string {
  return PAUSE_REASON_LABELS[reason] ?? reason;
}

export function AgentCard({
  id,
  name,
  role,
  status,
  x,
  y,
  title,
  capabilities,
  lastHeartbeatAt,
  pauseReason,
  errorReason,
}: AgentCardProps) {
  // The detail line under the name: prefer the agent's title, else the role.
  const subtitle = title?.trim() || role;

  // A short status descriptor. When paused/errored and a reason is present,
  // append it so the card explains *why* without opening the detail page.
  // Pause codes are humanized; error reasons are already prose.
  const reason =
    status === "error"
      ? errorReason?.trim()
      : status === "paused" && pauseReason?.trim()
        ? pauseReasonLabel(pauseReason.trim())
        : null;
  const statusText = reason ? `${statusLabel(status)} · ${reason}` : statusLabel(status);

  return (
    <Link
      href={agentInspectorUrl(id)}
      prefetch={false}
      onClick={(e) => handleShellLinkClick(e, agentInspectorUrl(id))}
      data-agent-card-id={id}
      data-org-card
      className={cn(
        "editorial-card editorial-card-hover absolute flex flex-col gap-1.5 p-3 select-none",
        "no-underline overflow-hidden",
      )}
      style={{
        left: `${x}px`,
        top: `${y}px`,
        width: `${CARD_W}px`,
        height: `${CARD_H}px`,
        color: "var(--midnight)",
      }}
    >
      {/* Header: glyph avatar + name + status dot */}
      <div className="flex items-center gap-2 min-w-0">
        <span className="relative shrink-0">
          <span
            className="flex h-7 w-7 items-center justify-center rounded-full"
            style={{ backgroundColor: "var(--cream-deep)" }}
          >
            <Bot size={16}
              style={{ color: "var(--midnight-muted)" }}
              aria-hidden="true"
            />
          </span>
          <span
            className={cn(
              "absolute -bottom-0.5 -right-0.5 h-2.5 w-2.5 rounded-full",
              statusDotClass(status),
            )}
            style={{
              backgroundColor: statusDotColor(status),
              border: "2px solid var(--cream)",
            }}
            aria-label={`Status: ${status}`}
            title={statusText}
          />
        </span>
        <span className="font-editorial-serif text-base truncate flex-1 leading-tight">
          {name}
        </span>
      </div>

      {/* Subtitle: title or role */}
      <div className="text-xs capitalize truncate" style={{ color: "var(--midnight-muted)" }}>
        {subtitle}
      </div>

      {/* Capabilities (freeform), two-line clamp */}
      {capabilities?.trim() ? (
        <div
          className="text-caption leading-snug line-clamp-2"
          style={{ color: "var(--midnight-soft)" }}
        >
          {capabilities}
        </div>
      ) : null}

      {/* Footer: status text + last-active, pinned to the bottom edge */}
      <div className="mt-auto flex items-center justify-between gap-2 text-2xs">
        <span className="truncate" style={{ color: "var(--midnight-muted)" }} title={statusText}>
          {statusText}
        </span>
        {lastHeartbeatAt ? (
          <span
            className="shrink-0 whitespace-nowrap"
            style={{ color: "var(--midnight-muted)" }}
            title={new Date(lastHeartbeatAt).toLocaleString()}
          >
            {timeAgo(lastHeartbeatAt)}
          </span>
        ) : null}
      </div>
    </Link>
  );
}
