// apps/frontend/src/components/teams/issues/BlockedReasonChip.tsx

// Ported from upstream Paperclip's BlockedReasonChip
// (paperclip/ui/src/components/BlockedReasonChip.tsx @ v2026.1012.0-isol8.4)
// (MIT, (c) 2025 Paperclip AI). Props, icons, label and aria-label are verbatim.
//
// Adaptations vs upstream (only what had to change):
//   - @paperclipai/shared types        -> components/teams/shared/types
//   - violet / cyan / amber / slate / red palette -> brand state tokens
//                                         info / warn / err, and the neutral
//                                         edge / cream-deep / midnight-soft ladder
//                                         for external_wait (.dark re-points them,
//                                         so dark: variants collapse)
//   - severity dots red-500 / orange-500 -> err / warn
//   - --text-nano / --text-micro       -> text-caption

import { AlertTriangle, Clock, Pause, User, Wrench } from "lucide-react";
import type { ComponentType } from "react";
import { cn } from "@/lib/utils";
import type {
  IssueBlockedInboxReason,
  IssueBlockedInboxSeverity,
} from "@/components/teams/shared/types";
import {
  blockedReasonVariant,
  blockedVariantLabel,
  type BlockedReasonVariant,
} from "./lib/blockedInbox";

interface BlockedReasonChipProps {
  reason: IssueBlockedInboxReason;
  severity: IssueBlockedInboxSeverity;
  compact?: boolean;
  className?: string;
}

type IconComponent = ComponentType<{ className?: string; "aria-hidden"?: boolean | "true" | "false" }>;

const VARIANT_STYLES: Record<BlockedReasonVariant, string> = {
  needs_decision: "border-info/40 bg-info/10 text-info",
  recovery_required: "border-info/40 bg-info/5 text-info",
  stalled: "border-warn/60 bg-warn/15 text-warn",
  needs_attention: "border-warn/40 bg-warn/10 text-warn",
  external_wait: "border-[var(--edge)] bg-[var(--cream-deep)] text-[var(--midnight-soft)]",
  owner_paused: "border-err/40 bg-err/10 text-err",
};

const VARIANT_ICONS: Record<BlockedReasonVariant, IconComponent> = {
  needs_decision: Clock,
  recovery_required: Wrench,
  stalled: AlertTriangle,
  needs_attention: AlertTriangle,
  external_wait: User,
  owner_paused: Pause,
};

const SEVERITY_DOT: Partial<Record<IssueBlockedInboxSeverity, string>> = {
  critical: "bg-err",
  high: "bg-warn",
};

export function BlockedReasonChip({
  reason,
  severity,
  compact = false,
  className,
}: BlockedReasonChipProps) {
  const variant = blockedReasonVariant(reason);
  const label = blockedVariantLabel(variant);
  const Icon = VARIANT_ICONS[variant];
  const dotClass = SEVERITY_DOT[severity];
  return (
    <span
      data-testid="blocked-reason-chip"
      data-variant={variant}
      data-severity={severity}
      aria-label={`Reason: ${label}, severity ${severity}`}
      className={cn(
        "inline-flex shrink-0 items-center gap-1 rounded-md border px-2 py-0.5 text-caption font-medium leading-tight",
        VARIANT_STYLES[variant],
        className,
      )}
    >
      {dotClass ? (
        <span
          aria-hidden="true"
          className={cn("inline-block h-1.5 w-1.5 shrink-0 rounded-full", dotClass)}
        />
      ) : null}
      {compact ? null : <Icon className="h-3 w-3 shrink-0" aria-hidden="true" />}
      <span className="truncate">{label}</span>
    </span>
  );
}
