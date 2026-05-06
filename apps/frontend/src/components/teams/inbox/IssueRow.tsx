// Ported from upstream Paperclip's IssueRow.tsx
// (paperclip/ui/src/components/IssueRow.tsx) (MIT, (c) 2025 Paperclip AI).
// See spec at docs/superpowers/specs/2026-05-04-teams-inbox-deep-port-design.md

import type { ReactNode } from "react";
import Link from "next/link";
import { Eye, X } from "lucide-react";
import type { Issue } from "@/components/teams/shared/types";
import {
  createIssueDetailPath,
  rememberIssueDetailLocationState,
  withIssueDetailHeaderSeed,
} from "@/components/teams/shared/lib/issueDetailBreadcrumb";
import { cn } from "@/lib/utils";
import { StatusIcon } from "@/components/teams/shared/components/StatusIcon";
import { productivityReviewTriggerLabel } from "@/components/teams/shared/components/ProductivityReviewBadge";

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
}: IssueRowProps) {
  const issuePathId = issue.identifier ?? issue.id;
  const identifier = issue.identifier ?? issue.id.slice(0, 8);
  const showUnreadSlot = unreadState !== null;
  const showUnreadDot = unreadState === "visible" || unreadState === "fading";
  const selectedStatusClass = selected
    ? "!text-muted-foreground !border-muted-foreground"
    : undefined;
  const detailState = withIssueDetailHeaderSeed(issueLinkState, issue);
  const productivityReview = issue.productivityReview ?? null;
  // StatusIcon expects the rich `IssueBlockerAttention` shape; the slim Issue
  // type also permits a boolean for back-compat. Coerce booleans to undefined
  // so the icon falls through to its default attention-less rendering.
  const blockerAttention =
    typeof issue.blockerAttention === "object" ? issue.blockerAttention : null;
  const productivityReviewIndicator = productivityReview ? (
    // Retheme: amber-500 productivity-review chip → editorial warn token.
    <span
      className={cn(
        "inline-flex h-4 w-4 shrink-0 items-center justify-center rounded-full border",
        selected ? "border-muted-foreground text-muted-foreground" : null,
      )}
      style={
        selected
          ? undefined
          : {
              borderColor: "rgba(196, 122, 29, 0.4)",
              background: "rgba(196, 122, 29, 0.1)",
              color: "var(--warn)",
            }
      }
      title={`Productivity review: ${productivityReviewTriggerLabel(productivityReview.trigger)}`}
      aria-label="Productivity review open"
    >
      <Eye className="h-2.5 w-2.5" aria-hidden />
    </span>
  ) : null;
  const hasChecklistStep = checklistStepNumber !== null;
  const checklistStep = hasChecklistStep ? (
    <span
      className="shrink-0 font-mono text-xs text-muted-foreground"
      aria-hidden="true"
    >
      {checklistStepNumber}.
    </span>
  ) : null;

  return (
    // TODO(#3d): re-evaluate quicklook/prefetch/state props when IssueDetail page lands.
    <Link
      href={createIssueDetailPath(issuePathId)}
      data-inbox-issue-link
      id={checklistRowId}
      aria-current={checklistCurrentStep ? "step" : undefined}
      onClickCapture={() =>
        rememberIssueDetailLocationState(issuePathId, detailState)
      }
      className={cn(
        "group flex items-start gap-2 border-b border-border py-2.5 pl-2 pr-3 text-sm no-underline text-inherit transition-colors last:border-b-0 sm:items-center sm:py-2 sm:pl-1",
        selected ? "hover:bg-transparent" : "hover:bg-accent/50",
        checklistCurrentStep
          ? "border-l-2 border-l-primary bg-primary/5 pl-[calc(theme(spacing.2)-2px)] sm:pl-[calc(theme(spacing.1)-2px)]"
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
                {productivityReviewIndicator}
              </span>
              {checklistStep}
              <span className="shrink-0 font-mono text-xs text-muted-foreground">
                {identifier}
              </span>
            </>
          )}
          {mobileMeta ? (
            <>
              <span
                className="text-xs text-muted-foreground sm:hidden"
                aria-hidden="true"
              >
                &middot;
              </span>
              <span className="text-xs text-muted-foreground sm:hidden">
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
            <span className="text-xs text-muted-foreground">{trailingMeta}</span>
          ) : null}
        </span>
      ) : null}
      {showUnreadSlot ? (
        <span className="inline-flex h-4 w-4 shrink-0 items-center justify-center self-center">
          {showUnreadDot ? (
            <button
              type="button"
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
                "inline-flex h-4 w-4 items-center justify-center rounded-full transition-colors",
                selected
                  ? "hover:bg-muted/80"
                  : "hover:bg-[var(--forest-tint)]",
              )}
              aria-label="Mark as read"
            >
              {/* Retheme: amber-700 unread dot → editorial forest token. */}
              <span
                className={cn(
                  "block h-2 w-2 rounded-full transition-opacity duration-300",
                  unreadState === "fading" ? "opacity-0" : "opacity-100",
                )}
                style={{
                  background: selected
                    ? "var(--mute-soft)"
                    : "var(--forest)",
                }}
              />
            </button>
          ) : onArchive ? (
            <button
              type="button"
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
              className="inline-flex h-4 w-4 items-center justify-center rounded-md text-muted-foreground opacity-0 transition-opacity hover:text-foreground group-hover:opacity-100 disabled:pointer-events-none disabled:opacity-30"
              aria-label="Dismiss from inbox"
            >
              <X className="h-3.5 w-3.5" />
            </button>
          ) : (
            <span className="inline-flex h-4 w-4" aria-hidden="true" />
          )}
        </span>
      ) : null}
    </Link>
  );
}
