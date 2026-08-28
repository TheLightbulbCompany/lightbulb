"use client";

import { useState } from "react";
import { MoreHorizontal } from "lucide-react";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useIssueMutations } from "@/components/teams/issues/hooks/useIssueMutations";
import type { Issue, IssueStatus } from "@/components/teams/shared/types";

// Close/cancel/reopen are issue status transitions (no dedicated endpoints).
const CLOSE_STATUS: IssueStatus = "done";
const CANCEL_STATUS: IssueStatus = "cancelled";
const REOPEN_STATUS: IssueStatus = "todo";

export function IssueStatusMenu({
  issue,
  onChanged,
}: {
  issue: Issue;
  onChanged?: () => void;
}) {
  const { update } = useIssueMutations();
  const [busy, setBusy] = useState(false);
  const resolved = issue.status === "done" || issue.status === "cancelled";

  async function setStatus(status: IssueStatus) {
    if (busy) return;
    setBusy(true);
    try {
      await update(issue.id, { status });
      onChanged?.();
    } catch {
      // best-effort: on failure the list simply won't change
    } finally {
      setBusy(false);
    }
  }

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          aria-label="Issue actions"
          disabled={busy}
          // The inbox row is a whole-row <Link>; prevent the trigger click from
          // navigating it (mirrors IssueRow's archive button). Radix portals the
          // menu content out, so item clicks never reach the Link.
          onClick={(e) => {
            e.preventDefault();
            e.stopPropagation();
          }}
          className="inline-flex h-6 w-6 items-center justify-center rounded text-[var(--midnight-muted)] hover:bg-[var(--edge)] disabled:opacity-40"
        >
          <MoreHorizontal className="h-4 w-4" />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        {resolved ? (
          <DropdownMenuItem onSelect={() => void setStatus(REOPEN_STATUS)}>
            Reopen
          </DropdownMenuItem>
        ) : (
          <>
            <DropdownMenuItem onSelect={() => void setStatus(CLOSE_STATUS)}>
              Close
            </DropdownMenuItem>
            <DropdownMenuItem onSelect={() => void setStatus(CANCEL_STATUS)}>
              Cancel
            </DropdownMenuItem>
          </>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
