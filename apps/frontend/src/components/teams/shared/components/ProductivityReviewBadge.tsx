// apps/frontend/src/components/teams/shared/components/ProductivityReviewBadge.tsx

// Ported from upstream Paperclip's ProductivityReviewBadge.tsx
// (paperclip/ui/src/components/ProductivityReviewBadge.tsx) (MIT, (c) 2025
// Paperclip AI). Two deviations from upstream:
//   * React-Router `Link to=` -> next/link `href=`
//   * Upstream's <Tooltip> wrapper requires the shadcn tooltip primitive +
//     @radix-ui/react-tooltip, neither of which exist in the Isol8 frontend
//     yet. Until they land we fall back to a native `title=` attribute on
//     the link so the trigger label still surfaces on hover. The richer
//     multi-line popover content is preserved in the JSX below but rendered
//     inline-hidden — once Tooltip is vendored, swap the `<>` wrapper for
//     <Tooltip><TooltipTrigger asChild>...</TooltipTrigger><TooltipContent>
//     ...</TooltipContent></Tooltip> and drop the title attribute.
// Editorial retheme: the upstream amber tokens (border-amber-500/40,
// bg-amber-500/10, text-amber-700) map onto `--warn` in the editorial
// palette — the badge still reads as "attention required" against the
// cream surface, but in the right hue for the new chrome.

import Link from "next/link";
import { Eye } from "lucide-react";
import { cn } from "@/lib/utils";
import { createIssueDetailPath } from "@/components/teams/shared/lib/issueDetailBreadcrumb";
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
  const reviewPath = createIssueDetailPath(
    review.reviewIdentifier ?? review.reviewIssueId,
  );
  const statusLabel =
    REVIEW_STATUS_LABELS[review.status] ?? review.status.replace(/_/g, " ");

  const noCommentStreakLine =
    typeof review.noCommentStreak === "number" && review.noCommentStreak > 0
      ? `\nNo-comment streak: ${review.noCommentStreak} runs`
      : "";
  const titleText = `Productivity review open\nTrigger: ${label}${noCommentStreakLine}\nReview: ${reviewIdentifier} (${statusLabel})`;

  return (
    <Link
      href={reviewPath}
      className={cn(
        "inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[10px] font-medium shrink-0 transition-colors",
        className,
      )}
      style={{
        borderColor: "color-mix(in srgb, var(--warn) 40%, transparent)",
        backgroundColor: "color-mix(in srgb, var(--warn) 12%, transparent)",
        color: "var(--warn)",
      }}
      aria-label={`Under review · productivity review ${reviewIdentifier} (${label})`}
      title={titleText}
    >
      <Eye className="h-3 w-3" aria-hidden />
      {hideLabel ? null : <span>Under review</span>}
    </Link>
  );
}
