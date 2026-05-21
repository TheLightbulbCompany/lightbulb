// apps/frontend/src/components/teams/shared/components/PageSkeleton.tsx

// Ported from upstream Paperclip's components/PageSkeleton.tsx
// (paperclip/ui/src/components/PageSkeleton.tsx) (MIT, (c) 2025 Paperclip AI).
// v1: only the 'inbox' variant. Other variants deferred until consumers exist.

import type { JSX } from "react";
import { cn } from "@/lib/utils";

export interface PageSkeletonProps {
  variant?: "inbox";
  /** Number of placeholder rows. Defaults to 5. */
  rowCount?: number;
}

/**
 * Loading placeholder for the Teams Inbox page. Mirrors the visual rhythm of
 * IssueRow (status-icon + identifier-pill + title + trailing time) so the
 * shimmer doesn't reflow when real content arrives.
 *
 * Uses editorial palette tokens (`--cream-deeper`, `--cream-deepest`,
 * `--edge`) so the placeholder reads as paper rather than gray on the cream
 * surface.
 */
export function PageSkeleton({
  variant = "inbox",
  rowCount = 5,
}: PageSkeletonProps): JSX.Element {
  // Only `inbox` is implemented; the prop is reserved for future variants.
  void variant;

  return (
    <div className="space-y-4 animate-pulse" data-testid="page-skeleton">
      {/* Header bar: title placeholder + trailing action placeholder */}
      <div className="flex items-center justify-between">
        <div
          className="h-6 w-40 rounded"
          style={{ backgroundColor: "var(--cream-deepest)" }}
        />
        <div
          className="h-6 w-24 rounded"
          style={{ backgroundColor: "var(--cream-deeper)" }}
        />
      </div>

      {/* Row stack — same h-10 rhythm as IssueRow */}
      <div className="space-y-1">
        {Array.from({ length: rowCount }).map((_, i) => (
          <div
            key={i}
            data-skeleton-row
            className={cn("flex h-10 items-center gap-3 px-3 border rounded-none")}
            style={{ borderColor: "var(--edge)" }}
          >
            {/* status-icon placeholder */}
            <div
              className="h-4 w-4 shrink-0 rounded-full"
              style={{ backgroundColor: "var(--cream-deepest)" }}
            />
            {/* identifier-pill placeholder */}
            <div
              className="h-3 w-12 shrink-0 rounded"
              style={{ backgroundColor: "var(--cream-deeper)" }}
            />
            {/* title bar — flex-grow */}
            <div
              className="h-3 flex-1 rounded"
              style={{ backgroundColor: "var(--cream-deepest)" }}
            />
            {/* trailing time placeholder */}
            <div
              className="h-3 w-10 shrink-0 rounded"
              style={{ backgroundColor: "var(--cream-deeper)" }}
            />
          </div>
        ))}
      </div>
    </div>
  );
}
