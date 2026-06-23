"use client";

// Ported + enriched from upstream Paperclip's OrgChart card render
// (paperclip/ui/src/pages/OrgChart.tsx) (MIT, (c) 2025 Paperclip AI).
//
// Upstream shows avatar + status dot, name, title (falling back to a role
// label), the adapter label, and capabilities (line-clamped). We mirror that
// in the editorial theme, minus the adapter label (the BFF strips
// `adapterType` from the agents list as a token-leak defense — see
// routers/teams/agents.py `_redact_agent`), and add a relative "last active"
// line plus the pause/error reason when the status carries one.
//
// Click navigates to the agent detail. The `data-org-card` marker lets the
// pan/zoom viewport tell card clicks apart from drag-to-pan.

import Link from "next/link";
import { Bot } from "lucide-react";
import { cn } from "@/lib/utils";
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
  /** Avatar image URL; falls back to the Bot glyph when absent. */
  iconUrl?: string | null;
  /** ISO timestamp → rendered as a relative "active <n> ago" line. */
  lastHeartbeatAt?: string | null;
  /** Shown beside the status when paused. */
  pauseReason?: string | null;
  /** Shown beside the status when errored. */
  errorReason?: string | null;
}

/** Humanize a status token for the secondary status line. */
function statusLabel(status: string): string {
  if (!status) return "Idle";
  return status.charAt(0).toUpperCase() + status.slice(1);
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
  iconUrl,
  lastHeartbeatAt,
  pauseReason,
  errorReason,
}: AgentCardProps) {
  // The detail line under the name: prefer the agent's title, else the role.
  const subtitle = title?.trim() || role;

  // A short status descriptor. When paused/errored and a reason is present,
  // append it so the card explains *why* without opening the detail page.
  const reason =
    status === "error"
      ? errorReason?.trim()
      : status === "paused"
        ? pauseReason?.trim()
        : null;
  const statusText = reason ? `${statusLabel(status)} · ${reason}` : statusLabel(status);

  return (
    <Link
      href={`/teams/agents/${id}`}
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
        color: "var(--ink)",
      }}
    >
      {/* Header: avatar + name + status dot */}
      <div className="flex items-center gap-2 min-w-0">
        <span className="relative shrink-0">
          {iconUrl ? (
            // eslint-disable-next-line @next/next/no-img-element -- avatar URLs are
            // arbitrary external hosts; next/image would require remotePatterns config.
            <img
              src={iconUrl}
              alt=""
              className="h-7 w-7 rounded-full object-cover"
              style={{ border: "1px solid var(--edge)" }}
            />
          ) : (
            <span
              className="flex h-7 w-7 items-center justify-center rounded-full"
              style={{ backgroundColor: "var(--cream-deeper)" }}
            >
              <Bot
                className="h-4 w-4"
                style={{ color: "var(--mute)" }}
                aria-hidden="true"
              />
            </span>
          )}
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
      <div className="text-xs capitalize truncate" style={{ color: "var(--mute)" }}>
        {subtitle}
      </div>

      {/* Capabilities (freeform), two-line clamp */}
      {capabilities?.trim() ? (
        <div
          className="text-[11px] leading-snug line-clamp-2"
          style={{ color: "var(--ink-soft)" }}
        >
          {capabilities}
        </div>
      ) : null}

      {/* Footer: status text + last-active, pinned to the bottom edge */}
      <div className="mt-auto flex items-center justify-between gap-2 text-[10px]">
        <span className="truncate" style={{ color: "var(--mute)" }} title={statusText}>
          {statusText}
        </span>
        {lastHeartbeatAt ? (
          <span
            className="shrink-0 whitespace-nowrap"
            style={{ color: "var(--mute-soft)" }}
            title={new Date(lastHeartbeatAt).toLocaleString()}
          >
            {timeAgo(lastHeartbeatAt)}
          </span>
        ) : null}
      </div>
    </Link>
  );
}
