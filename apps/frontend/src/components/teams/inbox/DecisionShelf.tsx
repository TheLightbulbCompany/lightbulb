// apps/frontend/src/components/teams/inbox/DecisionShelf.tsx

// Ported from upstream Paperclip's DecisionShelf
// (paperclip/ui/src/components/DecisionShelf.tsx @ v2026.1012.0-isol8.4)
// (MIT, (c) 2025 Paperclip AI). The curtain ("Label (count)", collapsed until
// opened) and the aging row's "Idle N days" label are verbatim.
//
// Adaptations vs upstream (only what had to change):
//   - IssueGroupHeader          -> an inline collapsible header button
//   - "Keep on desk"            -> not ported: it writes decision-queue retention,
//                                  which the BFF does not proxy
//   - agents / triage props     -> dropped with the triage strip

"use client";

import type { ReactNode } from "react";
import { ChevronRight } from "lucide-react";
import { cn } from "@/lib/utils";
import { attentionIdleDays } from "@/components/teams/inbox/lib/attention";
import { AttentionQueueRow } from "@/components/teams/inbox/AttentionQueueRow";
import type { AttentionItem, CompanyAgent } from "@/components/teams/shared/types";

/** A collapsible shelf header + body (snoozed / dismissed / aging). */
export function Curtain({
  label,
  count,
  open,
  onToggle,
  children,
}: {
  label: string;
  count?: number | string;
  open: boolean;
  onToggle: () => void;
  children: ReactNode;
}) {
  return (
    <section className="flex flex-col gap-2">
      <ShelfHeader label={count == null ? label : `${label} (${count})`} collapsed={!open} onToggle={onToggle} />
      {open && <div className="flex flex-col gap-4">{children}</div>}
    </section>
  );
}

/** The collapsible header every desk shelf and curtain uses. */
export function ShelfHeader({
  label,
  collapsed,
  onToggle,
  trailing,
}: {
  label: string;
  collapsed: boolean;
  onToggle: () => void;
  trailing?: ReactNode;
}) {
  return (
    <button type="button" onClick={onToggle} aria-expanded={!collapsed} className="flex w-full items-center gap-1 text-left">
      <ChevronRight
        size={12}
        aria-hidden="true"
        className={cn("shrink-0 text-[var(--fg-mute)] transition-transform", !collapsed && "rotate-90")}
      />
      <span className="label-caps text-[var(--fg-mute)]">{label}</span>
      {trailing && <span className="ml-auto text-caption tabular-nums text-[var(--fg-mute)]">{trailing}</span>}
    </button>
  );
}

/** An aging-shelf row: the standard card, prefaced by how long it has been idle. */
export function AgingItemRow({
  item,
  now,
  agentMap,
  currentUserId,
  expanded,
  onToggleExpand,
  onDismiss,
  onSnooze,
}: {
  item: AttentionItem;
  now: number;
  agentMap: Map<string, CompanyAgent>;
  currentUserId?: string | null;
  expanded: boolean;
  onToggleExpand: (item: AttentionItem) => void;
  onDismiss: (item: AttentionItem) => void;
  onSnooze: (item: AttentionItem, snoozedUntil: string) => void;
}) {
  const idleDays = attentionIdleDays(item, now);
  return (
    <div className="flex flex-col gap-1.5">
      <span className="px-1 text-caption text-[var(--fg-mute)]">
        Idle {idleDays} {idleDays === 1 ? "day" : "days"}
      </span>
      <AttentionQueueRow
        item={item}
        expanded={expanded}
        onToggleExpand={onToggleExpand}
        onDismiss={onDismiss}
        onSnooze={onSnooze}
        agentMap={agentMap}
        currentUserId={currentUserId}
      />
    </div>
  );
}
