// apps/frontend/src/components/teams/issues/IssueMonitorBanner.tsx

// Ported from upstream Paperclip's IssueMonitorBanner
// (paperclip/ui/src/components/IssueMonitorBanner.tsx @ v2026.916.1-isol8.2)
// (MIT, (c) 2025 Paperclip AI). Copy, state derivation and both surfaces
// (top banner + composer strip) are verbatim.
//
// Adaptations vs upstream (only what had to change):
//   - @paperclipai/shared Issue      -> components/teams/shared/types
//   - @/lib/issue-monitor            -> ./lib/issue-monitor
//   - @/components/InlineBanner      -> inlined below (this file is its only user);
//                                       brandBanner's hex palette -> brand state
//                                       tokens info/warn/err (.dark re-points them)

"use client";

import { useMemo } from "react";
import type { ReactNode } from "react";
import type { LucideIcon } from "lucide-react";
import { AlertCircle, AlertTriangle, Clock, Info } from "lucide-react";
import type { Issue } from "@/components/teams/shared/types";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import {
  deriveMonitorState,
  formatMonitorAbsolute,
  formatMonitorEta,
  useMonitorCountdown,
  type DerivedMonitorState,
  type MonitorDisplayState,
} from "./lib/issue-monitor";

// --- ui/src/components/InlineBanner.tsx (+ status-colors brandBanner) ---------

type BannerTone = "info" | "warning" | "danger";

const brandBanner: Record<BannerTone, string> = {
  info: "border-info/40 bg-info/10 text-info",
  warning: "border-warn/50 bg-warn/10 text-warn",
  danger: "border-err/40 bg-err/10 text-err",
};

const TONE_ICON: Record<BannerTone, LucideIcon> = {
  info: Info,
  warning: AlertTriangle,
  danger: AlertCircle,
};

interface InlineBannerProps {
  tone?: BannerTone;
  title?: ReactNode;
  children?: ReactNode;
  icon?: LucideIcon | false;
  actions?: ReactNode;
  compact?: boolean;
  className?: string;
}

function InlineBanner({
  tone = "info",
  title,
  children,
  icon,
  actions,
  compact = false,
  className,
}: InlineBannerProps) {
  const Icon = icon === false ? null : (icon ?? TONE_ICON[tone]);
  return (
    <div
      role="note"
      className={cn(
        "flex flex-col gap-2 rounded-lg border sm:flex-row sm:items-start sm:justify-between",
        compact ? "p-3" : "p-4",
        brandBanner[tone],
        className,
      )}
    >
      <div className="flex items-start gap-2.5">
        {Icon && <Icon className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />}
        <div className="space-y-1 text-ui">
          {title && <p className="font-medium leading-tight">{title}</p>}
          {children && <div className="leading-snug opacity-90">{children}</div>}
        </div>
      </div>
      {actions && (
        <div className="flex shrink-0 flex-wrap items-center gap-2 pl-6 sm:pl-0">{actions}</div>
      )}
    </div>
  );
}

// --- ui/src/components/IssueMonitorBanner.tsx ---------------------------------

/** Matches the `Date | string` inputs accepted by the issue-monitor helpers. */
type MonitorDate = Date | string;

/**
 * States in which the waiting-monitor surfaces (top banner + composer strip)
 * are shown. `cleared` and `none` hide both surfaces entirely — see
 * wireframe 04 (PAP-14557).
 */
const WAITING_STATES: readonly MonitorDisplayState[] = [
  "scheduled",
  "retrying",
  "due-now",
  "overdue",
];

export function isWaitingMonitorState(state: MonitorDisplayState): boolean {
  return WAITING_STATES.includes(state);
}

export function hasVisibleMonitorSurface(issue: Issue): boolean {
  const derived = deriveMonitorState(issue);
  return isWaitingMonitorState(derived.state) && derived.nextCheckAt !== null;
}

export interface MonitorSurfaceCopy {
  /** Prominent lead for the top banner, e.g. "Waiting on monitor — resumes in 2h 12m". */
  bannerTitle: string;
  /** Prominent lead for the composer strip, e.g. "Resumes in 2h 12m". */
  stripTitle: string;
  /** Muted detail line for the banner (absolute time carries a "(your time)" hint). */
  bannerMeta: string[];
  /** Muted detail line for the composer strip. */
  stripMeta: string[];
  /** `warning` (amber) once overdue, `info` (blue) while still on schedule. */
  tone: "info" | "warning";
  workspaceWait?: boolean;
}

function capitalize(value: string): string {
  return value.length === 0 ? value : `${value.charAt(0).toUpperCase()}${value.slice(1)}`;
}

/**
 * Pure copy builder shared by the banner and the composer strip so both
 * surfaces render one consistent copy system (see wireframe 04). Kept free of
 * hooks/`Date.now()` so it is deterministic under test.
 */
export function buildMonitorSurfaceCopy(
  derived: DerivedMonitorState,
  now: MonitorDate,
  scheduledRetryReason?: string | null,
): MonitorSurfaceCopy | null {
  if (!isWaitingMonitorState(derived.state) || !derived.nextCheckAt) return null;

  if (derived.source === "scheduled-retry" && scheduledRetryReason === "workspace_busy") {
    return {
      bannerTitle: "Waiting for workspace",
      stripTitle: "Waiting for workspace",
      bannerMeta: ["Another task is using this workspace. Work starts automatically when it is available."],
      stripMeta: ["Work starts automatically when the workspace is available."],
      tone: "info",
      workspaceWait: true,
    };
  }

  const eta = formatMonitorEta(derived.nextCheckAt, now); // "in 2h 12m" | "due now" | "overdue by 18m"
  const absolute = formatMonitorAbsolute(derived.nextCheckAt, {}, now); // local time, e.g. "Today, 4:08 PM"
  const isScheduledRetryOnly = derived.source === "scheduled-retry";

  let bannerTitle: string;
  let stripTitle: string;
  let statusHint: string | null = null;
  switch (derived.state) {
    case "scheduled":
    case "retrying":
      bannerTitle = isScheduledRetryOnly ? `Agent resumes ${eta}` : `Waiting on monitor — resumes ${eta}`;
      stripTitle = `Resumes ${eta}`;
      break;
    case "due-now":
      bannerTitle = isScheduledRetryOnly ? "Agent retry due now" : "Waiting on monitor — due now";
      stripTitle = "Due now";
      statusHint = "Checking momentarily…";
      break;
    case "overdue":
    default:
      bannerTitle = isScheduledRetryOnly ? `Agent retry ${eta}` : `Waiting on monitor — ${eta}`;
      stripTitle = capitalize(eta);
      statusHint = "Fires on next tick";
      break;
  }

  const attemptLabel = derived.attemptCount >= 1 ? `Attempt ${derived.attemptCount}` : null;
  const serviceLabel = derived.serviceName ? `Watching: ${derived.serviceName}` : null;

  const bannerMeta = [statusHint, `${absolute} (your time)`, attemptLabel, serviceLabel].filter(
    (piece): piece is string => Boolean(piece),
  );
  const stripMeta = [statusHint, absolute, attemptLabel, serviceLabel].filter(
    (piece): piece is string => Boolean(piece),
  );

  return {
    bannerTitle,
    stripTitle,
    bannerMeta,
    stripMeta,
    tone: derived.state === "overdue" ? "warning" : "info",
  };
}

function useMonitorSurfaceCopy(issue: Issue): MonitorSurfaceCopy | null {
  // `nextCheckAt` is stable for a given issue; derive once to seed the ticking
  // countdown cadence, then re-derive against the live clock so the surfaces
  // roll scheduled → due → overdue on their own.
  const nextCheckAt = useMemo(() => deriveMonitorState(issue).nextCheckAt, [issue]);
  const now = useMonitorCountdown(nextCheckAt);
  return useMemo(() => buildMonitorSurfaceCopy(deriveMonitorState(issue, now), now, issue.scheduledRetry?.scheduledRetryReason), [issue, now]);
}

function CheckNowButton({
  onCheckNow,
  checkingNow,
}: {
  onCheckNow: () => void;
  checkingNow: boolean;
}) {
  return (
    <Button
      type="button"
      variant="outline"
      size="sm"
      className="shrink-0 shadow-none"
      onClick={onCheckNow}
      disabled={checkingNow}
    >
      {checkingNow ? "Checking…" : "Check now"}
    </Button>
  );
}

export interface IssueMonitorSurfaceProps {
  issue: Issue;
  onCheckNow?: (() => void) | null;
  checkingNow?: boolean;
}

/**
 * Pinned banner rendered between the issue title and description while a
 * monitor is waiting. Replaces the description-area "Monitor scheduled" card
 * for the waiting state (PAP-14557 decision 1) — the two never render at once.
 */
export function IssueMonitorBanner({
  issue,
  onCheckNow = null,
  checkingNow = false,
}: IssueMonitorSurfaceProps) {
  const copy = useMonitorSurfaceCopy(issue);
  if (!copy) return null;

  return (
    <InlineBanner
      tone={copy.tone}
      icon={Clock}
      title={copy.bannerTitle}
      className="my-3"
      actions={onCheckNow && !copy.workspaceWait ? <CheckNowButton onCheckNow={onCheckNow} checkingNow={checkingNow} /> : null}
    >
      <span>{copy.bannerMeta.join("  ·  ")}</span>
    </InlineBanner>
  );
}

/**
 * Slim, inline (not sticky) strip anchored directly above the reply composer.
 * Mirrors the banner's monitor state and reminds the reader that replying wakes
 * the agent early (PAP-14557 decisions 2 + wireframe 02).
 */
export function IssueMonitorComposerStrip({
  issue,
  onCheckNow = null,
  checkingNow = false,
  className,
}: IssueMonitorSurfaceProps & { className?: string }) {
  const copy = useMonitorSurfaceCopy(issue);
  if (!copy) return null;

  return (
    <div
      role="note"
      data-testid="issue-monitor-composer-strip"
      className={cn("rounded-lg border border-border bg-muted/30 px-3 py-2", className)}
    >
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex min-w-0 items-start gap-2">
          <Clock className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" aria-hidden="true" />
          <div className="min-w-0">
            <div className="text-ui font-medium text-foreground">{copy.stripTitle}</div>
            <div className="text-caption text-muted-foreground">{copy.stripMeta.join(" · ")}</div>
          </div>
        </div>
        {onCheckNow && !copy.workspaceWait ? <CheckNowButton onCheckNow={onCheckNow} checkingNow={checkingNow} /> : null}
      </div>
      <p className="mt-1.5 text-caption text-muted-foreground">
        {copy.workspaceWait
          ? "You can keep sending instructions while the agent waits."
          : "Sending a reply wakes the agent now — before the scheduled check."}
      </p>
    </div>
  );
}
