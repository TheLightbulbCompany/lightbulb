"use client";

// Ported from upstream Paperclip's OrgChart card render
// (paperclip/ui/src/pages/OrgChart.tsx) (MIT, (c) 2025 Paperclip AI).
// Single agent card: name + role + status dot. Click navigates.

import Link from "next/link";
import { Bot } from "lucide-react";
import { cn } from "@/lib/utils";
import { statusDotClass } from "./statusColors";
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
        "absolute flex flex-col gap-1 rounded-lg border border-border bg-background p-3 shadow-sm transition-shadow hover:shadow-md",
        "no-underline text-foreground"
      )}
      style={{
        left: `${x}px`,
        top: `${y}px`,
        width: `${CARD_W}px`,
        height: `${CARD_H}px`,
      }}
    >
      <div className="flex items-center gap-2 min-w-0">
        <Bot className="h-4 w-4 text-muted-foreground shrink-0" aria-hidden="true" />
        <span className="font-medium text-sm truncate flex-1">{name}</span>
        <span
          className={cn("h-2 w-2 rounded-full shrink-0", statusDotClass(status))}
          aria-label={`Status: ${status}`}
          title={status}
        />
      </div>
      <div className="text-xs text-muted-foreground capitalize">{role}</div>
    </Link>
  );
}
