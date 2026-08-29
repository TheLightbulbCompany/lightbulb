// apps/frontend/src/components/teams/shared/components/ProductivityReviewBadge.tsx

// Ported from upstream Paperclip's ProductivityReviewBadge.tsx
// (paperclip/ui/src/components/ProductivityReviewBadge.tsx) (MIT, (c) 2025
// Paperclip AI). Two deviations from upstream:
//   * React-Router `Link to=` -> next/link `href=`
//   * Upstream's <Tooltip> wrapper is now the local `ui/tooltip` primitive,
//     which carries the multi-line trigger/review detail on hover and focus.
// Editorial retheme: the upstream badge's amber border, background wash,
// and label text (a 40%/10%-opacity amber-500 border/fill plus amber-700
// text) map onto `--warn` in the editorial palette — the badge still reads
// as "attention required" against the cream surface, but in the right hue
// for the new chrome.

import Link from "next/link";
import { Eye } from "lucide-react";
import { cn } from "@/lib/utils";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { handleShellLinkClick, teamsDetailUrl } from "@/lib/shellNav";
import type {
  IssueProductivityReview,
  IssueProductivityReviewTrigger,
} from "@/components/teams/shared/types";

// Re-export the rich productivity-review shapes (hoisted to types.ts so the
// `Issue.productivityReview` field can carry them). Existing imports from
// `./ProductivityReviewBadge` continue to work.
export type { IssueProductivityReview, IssueProductivityReviewTrigger };

const TRIGGER_LABELS: Record<string, string> = {
  no_comment_streak: "No-comment streak",
  long_active_duration: "Long active duration",
  high_churn: "High churn",
};

const REVIEW_STATUS_LABELS: Record<string, string> = {
  todo: "Open",
  in_progress: "In progress",
  in_review: "In review",
  blocked: "Blocked",
  backlog: "Open",
};

export function productivityReviewTriggerLabel(
  trigger: IssueProductivityReview["trigger"],
): string {
  if (!trigger) return "Productivity review";
  return TRIGGER_LABELS[trigger] ?? "Productivity review";
}

export function ProductivityReviewBadge({
  review,
  className,
  hideLabel = false,
}: {
  review: IssueProductivityReview;
  className?: string;
  hideLabel?: boolean;
}) {
  const label = productivityReviewTriggerLabel(review.trigger);
  const reviewIdentifier =
    review.reviewIdentifier ?? review.reviewIssueId.slice(0, 8);
  const reviewPath = teamsDetailUrl(
    "issue",
    review.reviewIdentifier ?? review.reviewIssueId,
  );
  const statusLabel =
    REVIEW_STATUS_LABELS[review.status] ?? review.status.replace(/_/g, " ");

  const noCommentStreakLine =
    typeof review.noCommentStreak === "number" && review.noCommentStreak > 0
      ? `\nNo-comment streak: ${review.noCommentStreak} runs`
      : "";
  const tooltipLines = [
    "Productivity review open",
    `Trigger: ${label}`,
    ...(noCommentStreakLine ? [noCommentStreakLine.trim()] : []),
    `Review: ${reviewIdentifier} (${statusLabel})`,
  ];

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Link
          href={reviewPath}
          prefetch={false}
          onClick={(e) => handleShellLinkClick(e, reviewPath)}
          className={cn(
            "inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-2xs font-medium shrink-0 transition-colors",
            className,
          )}
          style={{
            borderColor: "var(--warn)",
            backgroundColor: "var(--cream)",
            color: "var(--warn)",
          }}
          aria-label={`Under review · productivity review ${reviewIdentifier} (${label})`}
        >
          <Eye size={12} strokeWidth={1.75} aria-hidden />
          {hideLabel ? null : <span>Under review</span>}
        </Link>
      </TooltipTrigger>
      <TooltipContent>
        {tooltipLines.map((line) => (
          <span key={line} className="block">
            {line}
          </span>
        ))}
      </TooltipContent>
    </Tooltip>
  );
}
