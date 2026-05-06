"use client";

// Ported from upstream Paperclip's OrgChart card render
// (paperclip/ui/src/pages/OrgChart.tsx) (MIT, (c) 2025 Paperclip AI).
// Single agent card: name + role + status dot. Click navigates.

import Link from "next/link";
import { Bot } from "lucide-react";
import { cn } from "@/lib/utils";
import { statusDotClass, statusDotColor } from "./statusColors";
import { CARD_W, CARD_H } from "./orgChartLayout";

export interface AgentCardProps {
  id: string;
  name: string;
  role: string;
  status: string;
  x: number;
  y: number;
}

export function AgentCard({ id, name, role, status, x, y }: AgentCardProps) {
  return (
    <Link
      href={`/teams/agents/${id}`}
      data-agent-card-id={id}
      className={cn(
        "editorial-card editorial-card-hover absolute flex flex-col gap-1 p-3",
        "no-underline"
      )}
      style={{
        left: `${x}px`,
        top: `${y}px`,
        width: `${CARD_W}px`,
        height: `${CARD_H}px`,
        color: "var(--ink)",
      }}
    >
      <div className="flex items-center gap-2 min-w-0">
        <Bot
          className="h-4 w-4 shrink-0"
          style={{ color: "var(--mute)" }}
          aria-hidden="true"
        />
        <span className="font-editorial-serif text-base truncate flex-1">
          {name}
        </span>
        <span
          className={cn("h-2 w-2 rounded-full shrink-0", statusDotClass(status))}
          style={{ backgroundColor: statusDotColor(status) }}
          aria-label={`Status: ${status}`}
          title={status}
        />
      </div>
      <div
        className="text-xs capitalize"
        style={{ color: "var(--mute)" }}
      >
        {role}
      </div>
    </Link>
  );
}
