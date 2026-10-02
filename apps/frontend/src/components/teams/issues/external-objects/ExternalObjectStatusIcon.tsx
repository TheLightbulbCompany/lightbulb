// Ported from upstream Paperclip's ExternalObjectStatusIcon.tsx
// (paperclip/ui/src/components/ExternalObjectStatusIcon.tsx) (MIT, (c) 2025 Paperclip AI).

import { Clock } from "lucide-react";
import type {
  ExternalObjectLivenessState,
  ExternalObjectStatusCategory,
} from "@/components/teams/shared/types";
import { cn } from "@/lib/utils";
import {
  externalObjectCategoryLabel,
  externalObjectColor,
  externalObjectIconDefault,
  externalObjectIconsByCategory,
  externalObjectIconsByKey,
  externalObjectLivenessLabel,
} from "./external-objects";

interface ExternalObjectStatusIconProps {
  category: ExternalObjectStatusCategory;
  liveness: ExternalObjectLivenessState;
  statusIconKey?: string | null;
  /** Optional override label used in `aria-label` (e.g. provider-specific copy). */
  label?: string | null;
  className?: string;
  /** Tailwind size class — defaults to `h-3.5 w-3.5` (compact pill use). */
  sizeClassName?: string;
}

/**
 * Pure presentational icon for an external object's status. Combines:
 *
 * - A category icon (lucide) from `externalObjectIconsByCategory`.
 * - The status colour from `externalObjectColor`.
 * - An overlaid clock micro-mark when liveness is `stale`.
 * - A spinner for the `running` category, still under reduced motion.
 */
export function ExternalObjectStatusIcon({
  category,
  liveness,
  statusIconKey,
  label,
  className,
  sizeClassName = "h-3.5 w-3.5",
}: ExternalObjectStatusIconProps) {
  const Icon =
    externalObjectIconsByKey[statusIconKey ?? ""] ?? externalObjectIconsByCategory[category] ?? externalObjectIconDefault;
  const color = externalObjectColor(statusIconKey === "git-merge" ? "merged" : category);
  const livenessSuffix = liveness === "fresh" || liveness === "unknown"
    ? ""
    : ` (${externalObjectLivenessLabel(liveness)})`;
  const ariaLabel = `${label ?? externalObjectCategoryLabel(category)}${livenessSuffix}`;

  return (
    <span
      role="img"
      aria-label={ariaLabel}
      className={cn("relative inline-flex shrink-0", className)}
    >
      <Icon
        aria-hidden="true"
        className={cn(sizeClassName, category === "running" && "motion-safe:animate-spin")}
        style={{ color }}
      />
      {liveness === "stale" ? (
        <Clock
          aria-hidden="true"
          className="absolute -bottom-0.5 -right-0.5 h-2 w-2 rounded-full bg-[var(--plane-pane)] p-px text-[var(--fg-mute)]"
        />
      ) : null}
    </span>
  );
}
