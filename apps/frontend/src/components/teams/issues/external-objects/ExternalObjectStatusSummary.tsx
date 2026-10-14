// Ported from upstream Paperclip's ExternalObjectStatusSummary.tsx
// (paperclip/ui/src/components/ExternalObjectStatusSummary.tsx) (MIT, (c) 2025 Paperclip AI).

import type { ExternalObjectSummary } from "@/components/teams/shared/types";
import { cn } from "@/lib/utils";
import {
  dominantExternalObjectTone,
  externalObjectCategoryLabel,
  externalObjectColor,
  externalObjectDominantCount,
  externalObjectIconDefault,
  externalObjectIconsByCategory,
  externalObjectIconsByKey,
} from "./external-objects";

interface ExternalObjectStatusSummaryProps {
  summary: ExternalObjectSummary | null | undefined;
  /** Compact mode trims everything down to the icon + count, no label text. */
  compact?: boolean;
  className?: string;
}

function buildBreakdownTitle(summary: ExternalObjectSummary): string {
  const parts: string[] = [];
  for (const [category, count] of Object.entries(summary.byStatusCategory)) {
    if (!count) continue;
    parts.push(`${count} ${externalObjectCategoryLabel(category).toLowerCase()}`);
  }
  if (summary.staleCount > 0) parts.push(`${summary.staleCount} stale`);
  parts.push(`${summary.total} total`);
  return `External objects: ${parts.join(", ")}`;
}

/**
 * Compact rollup marker used by issue list rows. Renders the dominant severity
 * icon plus a count badge. Hidden when there are zero external objects or every
 * object is in a muted tone.
 */
export function ExternalObjectStatusSummary({
  summary,
  compact,
  className,
}: ExternalObjectStatusSummaryProps) {
  const tone = dominantExternalObjectTone(summary);
  const total = summary?.total ?? 0;
  if (!summary || total === 0 || !tone) return null;

  // Prefer the first object that matches the highestSeverity tone, since the
  // server has already ranked them.
  const object = summary.objects.find((item) => item.statusTone === summary.highestSeverity) ?? null;
  const category = object?.statusCategory ?? "unknown";
  const Icon =
    externalObjectIconsByKey[object?.statusIconKey ?? ""] ?? externalObjectIconsByCategory[category] ?? externalObjectIconDefault;
  const color = externalObjectColor(category);
  const dominantCount = externalObjectDominantCount(summary);
  const title = buildBreakdownTitle(summary);

  return (
    <span
      role="img"
      aria-label={title}
      title={title}
      className={cn(
        "inline-flex items-center gap-1 rounded-full px-1.5 py-0.5 text-2xs font-medium tabular-nums leading-none",
        compact && "px-1 py-0.5",
        className,
      )}
      style={{ color, background: `color-mix(in srgb, ${color} 15%, transparent)` }}
      data-external-status={category}
      data-external-tone={tone}
    >
      <Icon aria-hidden="true" className={cn("h-3 w-3 shrink-0", category === "running" && "motion-safe:animate-spin")} />
      <span>{dominantCount > 0 ? dominantCount : total}</span>
    </span>
  );
}
