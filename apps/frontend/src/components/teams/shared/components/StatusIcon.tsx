// apps/frontend/src/components/teams/shared/components/StatusIcon.tsx

// Ported from upstream Paperclip's StatusIcon.tsx
// (paperclip/ui/src/components/StatusIcon.tsx) (MIT, (c) 2025 Paperclip AI).
// See spec at docs/superpowers/specs/2026-05-04-teams-inbox-deep-port-design.md

import { useState } from "react";
import { cn } from "@/lib/utils";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Button } from "@/components/ui/button";
import type {
  IssueBlockerAttention,
  IssueBlockerAttentionReason,
  IssueBlockerAttentionState,
} from "@/components/teams/shared/types";

// Re-export the rich `IssueBlockerAttention` shape (hoisted to types.ts so
// the slim `Issue.blockerAttention` field can carry it). Existing imports
// from `./StatusIcon` continue to work.
export type {
  IssueBlockerAttention,
  IssueBlockerAttentionReason,
  IssueBlockerAttentionState,
};

// Inlined copy of the upstream `issueStatusIcon` map from
// `paperclip/ui/src/lib/status-colors.ts`, retheme-mapped onto the editorial
// palette. Each status resolves to a single CSS-variable color (used for both
// `color` and `borderColor`) so the swatch matches the rest of the Teams UI.
//   backlog / cancelled        -> --mute       (inactive / muted)
//   todo / in_progress         -> --warn       (attention / pending amber)
//   in_review                  -> --ink-soft   (neutral info)
//   done                       -> --forest     (success)
//   blocked                    -> --danger     (error)
const issueStatusColor: Record<string, string> = {
  backlog: "var(--mute)",
  todo: "var(--warn)",
  in_progress: "var(--warn)",
  in_review: "var(--ink-soft)",
  done: "var(--forest)",
  cancelled: "var(--mute)",
  blocked: "var(--danger)",
};

const issueStatusColorDefault = "var(--mute)";

// Blocked-state augmentations: covered (active dependency picking up the
// slack) reads as a softer forest signal; stalled (review without next step)
// reads as warning amber.
const blockedCoveredColor = "var(--forest-soft)";
const blockedStalledColor = "var(--warn)";

const allStatuses = [
  "backlog",
  "todo",
  "in_progress",
  "in_review",
  "done",
  "cancelled",
  "blocked",
];

function statusLabel(status: string): string {
  return status.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

interface StatusIconProps {
  status: string;
  blockerAttention?: IssueBlockerAttention | null;
  onChange?: (status: string) => void;
  className?: string;
  showLabel?: boolean;
}

function blockedAttentionLabel(
  blockerAttention: IssueBlockerAttention | null | undefined,
) {
  if (!blockerAttention || blockerAttention.state === "none") return "Blocked";

  if (blockerAttention.reason === "active_child") {
    const count = blockerAttention.coveredBlockerCount;
    if (count === 1 && blockerAttention.sampleBlockerIdentifier) {
      return `Blocked · waiting on active sub-issue ${blockerAttention.sampleBlockerIdentifier}`;
    }
    if (count === 1) return "Blocked · waiting on 1 active sub-issue";
    return `Blocked · waiting on ${count} active sub-issues`;
  }

  if (blockerAttention.reason === "active_dependency") {
    const count = blockerAttention.coveredBlockerCount;
    if (count === 1 && blockerAttention.sampleBlockerIdentifier) {
      return `Blocked · covered by active dependency ${blockerAttention.sampleBlockerIdentifier}`;
    }
    if (count === 1) return "Blocked · covered by 1 active dependency";
    return `Blocked · covered by ${count} active dependencies`;
  }

  if (blockerAttention.reason === "stalled_review") {
    const count = blockerAttention.stalledBlockerCount;
    const leaf =
      blockerAttention.sampleStalledBlockerIdentifier ??
      blockerAttention.sampleBlockerIdentifier;
    if (count === 1 && leaf) return `Blocked · review stalled on ${leaf}`;
    if (count === 1) return "Blocked · review stalled with no clear next step";
    return `Blocked · ${count} reviews stalled with no clear next step`;
  }

  if (blockerAttention.reason === "attention_required") {
    const count = blockerAttention.unresolvedBlockerCount;
    return `Blocked · ${count} unresolved ${count === 1 ? "blocker needs" : "blockers need"} attention`;
  }

  return "Blocked";
}

export function StatusIcon({
  status,
  blockerAttention,
  onChange,
  className,
  showLabel,
}: StatusIconProps) {
  const [open, setOpen] = useState(false);
  const isCoveredBlocked =
    status === "blocked" && blockerAttention?.state === "covered";
  const isStalledBlocked =
    status === "blocked" && blockerAttention?.state === "stalled";
  const swatchColor = isCoveredBlocked
    ? blockedCoveredColor
    : isStalledBlocked
      ? blockedStalledColor
      : (issueStatusColor[status] ?? issueStatusColorDefault);
  const isDone = status === "done";
  const ariaLabel =
    status === "blocked" ? blockedAttentionLabel(blockerAttention) : statusLabel(status);
  const blockerAttentionState = isCoveredBlocked
    ? "covered"
    : isStalledBlocked
      ? "stalled"
      : undefined;

  const circle = (
    <span
      className={cn(
        "relative inline-flex h-4 w-4 rounded-full border-2 shrink-0",
        onChange && !showLabel && "cursor-pointer",
        className,
      )}
      style={{ color: swatchColor, borderColor: swatchColor }}
      data-blocker-attention-state={blockerAttentionState}
      aria-label={ariaLabel}
      title={ariaLabel}
    >
      {isDone && (
        <span className="absolute inset-0 m-auto h-2 w-2 rounded-full bg-current" />
      )}
      {isCoveredBlocked && (
        <span
          className="absolute -bottom-0.5 -right-0.5 h-2 w-2 rounded-full border bg-current"
          style={{ borderColor: "var(--cream)" }}
        />
      )}
      {isStalledBlocked && (
        <span className="absolute inset-0 m-auto h-1.5 w-1.5 rounded-full bg-current" />
      )}
    </span>
  );

  if (!onChange)
    return showLabel ? (
      <span className="inline-flex items-center gap-1.5">
        {circle}
        <span className="text-sm">{statusLabel(status)}</span>
      </span>
    ) : (
      circle
    );

  const trigger = showLabel ? (
    <button className="inline-flex items-center gap-1.5 cursor-pointer hover:bg-accent/50 rounded px-1 -mx-1 py-0.5 transition-colors">
      {circle}
      <span className="text-sm">{statusLabel(status)}</span>
    </button>
  ) : (
    circle
  );

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>{trigger}</PopoverTrigger>
      <PopoverContent className="w-40 p-1" align="start">
        {allStatuses.map((s) => (
          <Button
            key={s}
            variant="ghost"
            size="sm"
            className={cn(
              "w-full justify-start gap-2 text-xs",
              s === status && "bg-accent",
            )}
            onClick={() => {
              onChange(s);
              setOpen(false);
            }}
          >
            <StatusIcon status={s} />
            {statusLabel(s)}
          </Button>
        ))}
      </PopoverContent>
    </Popover>
  );
}
