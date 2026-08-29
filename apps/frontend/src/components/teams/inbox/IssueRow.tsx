// Ported from upstream Paperclip's IssueRow.tsx
// (paperclip/ui/src/components/IssueRow.tsx) (MIT, (c) 2025 Paperclip AI).

import { useCallback, type MouseEvent, type ReactNode } from "react";
import Link from "next/link";
import { Eye, X } from "lucide-react";
import type { Issue } from "@/components/teams/shared/types";
import {
  rememberIssueDetailLocationState,
  withIssueDetailHeaderSeed,
} from "@/components/teams/shared/lib/issueDetailBreadcrumb";
import { teamsDetailUrl } from "@/lib/shellNav";
import { useInlineTeamsNav } from "@/components/chat/InlineTeamsNav";
import { cn } from "@/lib/utils";
import { StatusIcon } from "@/components/teams/shared/components/StatusIcon";
import { LiveRunDot } from "@/components/teams/shared/components/LiveRunDot";
import type { LiveIssueEntry } from "@/components/teams/shared/lib/liveRuns";
import { productivityReviewTriggerLabel } from "@/components/teams/shared/components/ProductivityReviewBadge";
import { EditorialButton } from "@/components/teams/shell";

type UnreadState = "hidden" | "visible" | "fading";

interface IssueRowProps {
  issue: Issue;
  // Upstream React-Router-only `state` prop is unsupported by next/link, so
  // we still accept it on the row API to satisfy callers but only feed it to
  // `withIssueDetailHeaderSeed` for the in-memory breadcrumb seed.
  issueLinkState?: unknown;
  selected?: boolean;
  mobileLeading?: ReactNode;
  desktopMetaLeading?: ReactNode;
  desktopLeadingSpacer?: boolean;
  mobileMeta?: ReactNode;
  desktopTrailing?: ReactNode;
  trailingMeta?: ReactNode;
  titleSuffix?: ReactNode;
  titleClassName?: string;
  checklistStepNumber?: number | string | null;
  checklistCurrentStep?: boolean;
  checklistDependencyChips?: ReactNode;
  checklistRowId?: string;
  unreadState?: UnreadState | null;
  onMarkRead?: () => void;
  onArchive?: () => void;
  archiveDisabled?: boolean;
  className?: string;
  /**
   * Live-run indicator for this issue: `running` (an agent is working now) or
   * `stalled` (the run has gone quiet). Absent when no run is live. `title`
   * carries the tooltip copy (silence age / next-action). Derived by the
   * caller from the `/inbox/live-runs` feed (see `buildLiveIssueState`).
   */
  live?: LiveIssueEntry | null;
}

export function IssueRow({
  issue,
  issueLinkState,
  selected = false,
  mobileLeading,
  desktopMetaLeading,
  desktopLeadingSpacer = false,
  mobileMeta,
  desktopTrailing,
  trailingMeta,
  titleSuffix,
  titleClassName,
  checklistStepNumber = null,
  checklistCurrentStep = false,
  checklistDependencyChips,
  checklistRowId,
  unreadState = null,
  onMarkRead,
  onArchive,
  archiveDisabled,
  className,
  live = null,
}: IssueRowProps) {
  // The detail view fetches upstream /api/issues/{id} through the BFF, which
  // resolves by the internal id ONLY — never the human-readable identifier
  // (e.g. ENG-42). So link by id; the identifier is display-only (see
  // `identifier` below).
  const issuePathId = issue.id;
  // Inside the /chat shell a nav provider is present → intercept the row's
  // own <Link> click and open the detail view in-shell. Without a provider
  // (hook returns null) the <Link href> drives navigation untouched — it is
  // the canonical in-shell URL, so middle-click / keyboard / direct-link all
  // still land in the right place.
  const nav = useInlineTeamsNav();
  const handleOpen = useCallback(
    (event: MouseEvent<HTMLAnchorElement>) => {
      if (!nav) return;
      // Only plain left-clicks route in-shell. Preserve native <Link>
      // behavior for modified / non-primary clicks (Cmd/Ctrl → new tab,
      // Shift → new window, middle-click) and any click another handler has
      // already handled — otherwise we'd swallow "open in new tab".
      if (
        event.defaultPrevented ||
        event.button !== 0 ||
        event.metaKey ||
        event.ctrlKey ||
        event.shiftKey ||
        event.altKey
      ) {
        return;
      }
      event.preventDefault();
      nav.openDetail("issue-detail", issuePathId);
    },
    [nav, issuePathId],
  );
  const identifier = issue.identifier ?? issue.id.slice(0, 8);
  const showUnreadSlot = unreadState !== null;
  const showUnreadDot = unreadState === "visible" || unreadState === "fading";
  const selectedStatusClass = selected
    ? "!text-[var(--midnight-muted)] !border-[var(--midnight-muted)]"
    : undefined;
  const detailState = withIssueDetailHeaderSeed(issueLinkState, issue);
  const productivityReview = issue.productivityReview ?? null;
  // StatusIcon expects the rich `IssueBlockerAttention` shape; the slim Issue
  // type also permits a boolean for back-compat. Coerce booleans to undefined
  // so the icon falls through to its default attention-less rendering.
  const blockerAttention =
    typeof issue.blockerAttention === "object" ? issue.blockerAttention : null;
  const productivityReviewIndicator = productivityReview ? (
    <span
      className={cn(
        "inline-flex h-4 w-4 shrink-0 items-center justify-center rounded-full border",
        selected ? "border-[var(--midnight-muted)] text-[var(--midnight-muted)]" : null,
      )}
      style={
        selected
          ? undefined
          : {
              borderColor: "var(--warn)",
              background: "var(--cream)",
              color: "var(--warn)",
            }
      }
      title={`Productivity review: ${productivityReviewTriggerLabel(productivityReview.trigger)}`}
      aria-label="Productivity review open"
    >
      <Eye size={12} strokeWidth={1.75} aria-hidden />
    </span>
  ) : null;
  const liveDot = live ? (
    <LiveRunDot state={live.activity} title={live.title} />
  ) : null;
  const hasChecklistStep = checklistStepNumber !== null;
  const checklistStep = hasChecklistStep ? (
    <span
      className="shrink-0 font-mono text-xs text-[var(--midnight-muted)]"
      aria-hidden="true"
    >
      {checklistStepNumber}.
    </span>
  ) : null;

  return (
    // TODO(#3d): re-evaluate quicklook/prefetch/state props when IssueDetail page lands.
    <Link
      href={teamsDetailUrl("issue", issuePathId)}
      prefetch={false}
      data-inbox-issue-link
      id={checklistRowId}
      aria-current={checklistCurrentStep ? "step" : undefined}
      onClickCapture={() =>
        rememberIssueDetailLocationState(issuePathId, detailState)
      }
      onClick={handleOpen}
      className={cn(
        "group flex items-start gap-2 border-b border-[var(--edge)] py-2.5 pl-2 pr-3 text-ui no-underline text-inherit transition-colors last:border-b-0 sm:items-center sm:py-2 sm:pl-1",
        selected
          ? "hover:bg-transparent"
          : checklistCurrentStep
            ? "hover:bg-[var(--cream-deeper)]"
            : "hover:bg-[var(--cream-deep)]",
        checklistCurrentStep
          ? "border-l-2 border-l-[var(--midnight)] bg-[var(--cream-deeper)] pl-[calc(theme(spacing.2)-2px)] sm:pl-[calc(theme(spacing.1)-2px)]"
          : null,
        className,
      )}
    >
      <span className="flex shrink-0 items-center gap-1 pt-px sm:hidden">
        {mobileLeading ?? (
          <StatusIcon
            status={issue.status}
            blockerAttention={blockerAttention}
            className={selectedStatusClass}
          />
        )}
        {liveDot}
        {productivityReviewIndicator}
      </span>
      <span className="flex min-w-0 flex-1 flex-col gap-1 sm:contents">
        <span
          className={cn(
            "line-clamp-2 text-sm sm:order-2 sm:min-w-0 sm:flex-1 sm:truncate sm:line-clamp-none",
            titleClassName,
          )}
        >
          {issue.title}
          {titleSuffix}
        </span>
        {checklistDependencyChips ? (
          <span className="flex flex-wrap gap-1 sm:order-3 sm:ml-[calc(theme(spacing.3)+theme(spacing.2))]">
            {checklistDependencyChips}
          </span>
        ) : null}
        <span className="flex items-center gap-2 sm:order-1 sm:shrink-0">
          {desktopLeadingSpacer ? (
            <span className="hidden w-3.5 shrink-0 sm:block" />
          ) : null}
          {desktopMetaLeading ?? (
            <>
              <span className="hidden shrink-0 items-center gap-1 sm:inline-flex">
                <StatusIcon
                  status={issue.status}
                  blockerAttention={blockerAttention}
                  className={selectedStatusClass}
                />
                {liveDot}
                {productivityReviewIndicator}
              </span>
              {checklistStep}
              <span className="shrink-0 font-mono text-xs text-[var(--midnight-muted)]">
                {identifier}
              </span>
            </>
          )}
          {mobileMeta ? (
            <>
              <span
                className="text-xs text-[var(--midnight-muted)] sm:hidden"
                aria-hidden="true"
              >
                &middot;
              </span>
              <span className="text-xs text-[var(--midnight-muted)] sm:hidden">
                {mobileMeta}
              </span>
            </>
          ) : null}
        </span>
      </span>
      {desktopTrailing || trailingMeta ? (
        <span className="ml-auto hidden shrink-0 items-center gap-2 sm:order-3 sm:flex sm:gap-3">
          {desktopTrailing}
          {trailingMeta ? (
            <span className="text-xs text-[var(--midnight-muted)]">{trailingMeta}</span>
          ) : null}
        </span>
      ) : null}
      {showUnreadSlot ? (
        <span className="inline-flex h-4 w-4 shrink-0 items-center justify-center self-center">
          {showUnreadDot ? (
            <EditorialButton
              variant="ghost"
              onClick={(event) => {
                event.preventDefault();
                event.stopPropagation();
                onMarkRead?.();
              }}
              onKeyDown={(event) => {
                if (event.key === "Enter" || event.key === " ") {
                  event.preventDefault();
                  event.stopPropagation();
                  onMarkRead?.();
                }
              }}
              className={cn(
                "inline-flex h-4 w-4 items-center justify-center rounded-full !p-0 !border-0 transition-colors",
                "hover:bg-[var(--cream-deep)]",
              )}
              aria-label="Mark as read"
            >
              <span
                className={cn(
                  "block h-2 w-2 rounded-full transition-opacity duration-300",
                  unreadState === "fading" ? "opacity-0" : "opacity-100",
                )}
                style={{
                  background: selected
                    ? "var(--midnight-soft)"
                    : "var(--midnight)",
                }}
              />
            </EditorialButton>
          ) : onArchive ? (
            <EditorialButton
              variant="ghost"
              onClick={(event) => {
                event.preventDefault();
                event.stopPropagation();
                onArchive();
              }}
              onKeyDown={(event) => {
                if (event.key !== "Enter" && event.key !== " ") return;
                event.preventDefault();
                event.stopPropagation();
                onArchive();
              }}
              disabled={archiveDisabled}
              className="inline-flex h-4 w-4 items-center justify-center rounded-md !p-0 !border-0 text-[var(--midnight-muted)] opacity-0 transition-opacity duration-(--dur-fast) ease-out hover:text-[var(--midnight)] group-hover:opacity-100 group-focus-within:opacity-100 focus-visible:opacity-100 disabled:pointer-events-none disabled:opacity-50"
              aria-label="Dismiss from inbox"
            >
              <X size={16} />
            </EditorialButton>
          ) : (
            <span className="inline-flex h-4 w-4" aria-hidden="true" />
          )}
        </span>
      ) : null}
    </Link>
  );
}
