// apps/frontend/src/components/teams/cases/CaseActivityFeed.tsx

// Ported from upstream Paperclip's CaseActivityFeed (paperclip/ui/src/components/CaseActivityFeed.tsx)
// (MIT, (c) 2025 Paperclip AI).

"use client";

import { useState } from "react";
import Link from "next/link";
import { Bot, User, Cog, ChevronDown, ListFilter } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { StatusIcon } from "@/components/teams/shared/components/StatusIcon";
import { timeAgo } from "@/components/teams/shared/lib/timeAgo";
import type { CaseEvent, CaseEventKind } from "@/components/teams/shared/types";
import { handleShellLinkClick } from "@/lib/shellNav";
import { cn } from "@/lib/utils";
import { issueHref } from "./caseNav";

const EVENT_LABEL: Record<CaseEventKind, string> = {
  created: "created",
  updated: "updated",
  fields_changed: "fields changed",
  status_changed: "status changed",
  issue_linked: "issue linked",
  issue_unlinked: "issue unlinked",
  document_revised: "document revised",
  child_linked: "child linked",
  attachment_added: "attachment added",
  label_added: "label added",
  label_removed: "label removed",
};

/** Human label for the actor, preferring the resolved agent name. */
function actorLabel(event: CaseEvent): string {
  if (event.actorType === "agent") return event.actorAgentName ?? "Agent";
  if (event.actorType === "user") return "User";
  return "System";
}

function ActorIcon({ event }: { event: CaseEvent }) {
  const Icon = event.actorType === "agent" ? Bot : event.actorType === "user" ? User : Cog;
  return <Icon className="h-3.5 w-3.5 shrink-0 text-[var(--fg-mute)]" aria-hidden />;
}

function issueRelationLabel(event: CaseEvent): string {
  return event.kind === "issue_linked" || event.kind === "issue_unlinked" ? "issue" : "via";
}

/** One event with actor + run→issue attribution. */
export function CaseEventRow({ event, compact = false }: { event: CaseEvent; compact?: boolean }) {
  const detail =
    event.kind === "status_changed" && event.payload
      ? `${(event.payload.previousStatus as string) ?? "?"} → ${(event.payload.status as string) ?? "?"}`
      : "";
  const issue = event.issue;
  return (
    <div className={cn("flex items-start gap-2 text-xs", compact ? "py-1.5" : "py-2")}>
      <span className="mt-1"><ActorIcon event={event} /></span>
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-baseline gap-x-1.5">
          <span className="font-medium">{EVENT_LABEL[event.kind] ?? event.kind}</span>
          {detail && <span className="text-[var(--fg-mute)]">· {detail}</span>}
        </div>
        <div className="flex flex-wrap items-center gap-x-1.5 text-[var(--fg-mute)]">
          <span>{actorLabel(event)}</span>
          {issue && (
            <>
              <span aria-hidden>·</span>
              <span>{issueRelationLabel(event)}</span>
              <Link
                href={issueHref(issue.id)}
                prefetch={false}
                onClick={(e) => handleShellLinkClick(e, issueHref(issue.id))}
                className="inline-flex min-w-0 items-center gap-1 text-[var(--fg-soft)] hover:underline"
                title={issue.title}
              >
                <StatusIcon status={issue.status} className="h-3 w-3" />
                <span className="shrink-0 font-mono">{issue.identifier}</span>
                <span className="min-w-0 truncate">{issue.title}</span>
              </Link>
            </>
          )}
          <span aria-hidden>·</span>
          <span>{timeAgo(event.createdAt)}</span>
        </div>
      </div>
    </div>
  );
}

/** The full activity feed with kind filters (detail-page Activity tab). */
export function CaseActivityFeed({ events }: { events: CaseEvent[] }) {
  const [active, setActive] = useState<Set<CaseEventKind>>(new Set());

  // Only offer filters for kinds actually present, in first-seen order.
  const presentKinds = [...new Set(events.map((e) => e.kind))];
  const filtered = active.size === 0 ? events : events.filter((e) => active.has(e.kind));

  function toggle(kind: CaseEventKind) {
    setActive((prev) => {
      const next = new Set(prev);
      if (next.has(kind)) next.delete(kind);
      else next.add(kind);
      return next;
    });
  }

  const filterLabel = active.size === 0
    ? "All activity"
    : active.size === 1
      ? EVENT_LABEL[[...active][0]!] ?? [...active][0]!
      : `${active.size} filters`;

  if (events.length === 0) {
    return <p className="py-6 text-center text-sm text-[var(--fg-mute)]">No activity yet.</p>;
  }

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between gap-2">
        <p className="text-xs text-[var(--fg-mute)]">
          {filtered.length} of {events.length} events
        </p>
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button variant="outline" size="sm" className="h-8 gap-1.5">
              <ListFilter className="h-3.5 w-3.5" />
              {filterLabel}
              <ChevronDown className="h-3.5 w-3.5 text-[var(--fg-mute)]" />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="w-56">
            <DropdownMenuLabel>Activity filter</DropdownMenuLabel>
            <DropdownMenuItem onSelect={() => setActive(new Set())}>
              All activity
            </DropdownMenuItem>
            <DropdownMenuSeparator />
            {presentKinds.map((kind) => (
              <DropdownMenuCheckboxItem
                key={kind}
                checked={active.has(kind)}
                onCheckedChange={() => toggle(kind)}
              >
                {EVENT_LABEL[kind] ?? kind}
              </DropdownMenuCheckboxItem>
            ))}
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
      {filtered.length === 0 ? (
        <p className="py-6 text-center text-sm text-[var(--fg-mute)]">No events match this filter.</p>
      ) : (
        <div className="divide-y divide-[var(--edge)]">
          {filtered.map((event) => (
            <CaseEventRow key={event.id} event={event} />
          ))}
        </div>
      )}
    </div>
  );
}
