"use client";

// Revision-history dropdown for a plan document (A3): pick the current or a
// historical revision to preview, or open the diff modal. Mirrors the "rev N ▾"
// control from upstream Paperclip's `IssueDocumentsSection`.

import { useMemo } from "react";
import { ChevronDown, GitCompare } from "lucide-react";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { timeAgo } from "@/components/teams/shared/lib/timeAgo";
import type { DocumentRevision } from "./lib/documentReviewTypes";

function actorOf(revision: DocumentRevision): string {
  return revision.createdByUserId ? "board" : revision.createdByAgentId ? "agent" : "system";
}

export function DocumentRevisionMenu({
  issueId,
  basePath = `/issues/${issueId}`,
  documentKey,
  latestRevisionNumber,
  selectedRevisionId,
  onSelectRevision,
  onOpenDiff,
}: {
  issueId: string;
  /** Owner of the document routes; a case passes `/cases/{id}`. */
  basePath?: string;
  documentKey: string;
  latestRevisionNumber: number;
  selectedRevisionId: string | null;
  onSelectRevision: (revisionId: string | null) => void;
  onOpenDiff: () => void;
}) {
  const { read } = useTeamsApi();
  const { data } = read<{ revisions: DocumentRevision[] }>(
    `${basePath}/documents/${documentKey}/revisions`,
  );
  const revisions = useMemo(
    () => [...(data?.revisions ?? [])].sort((a, b) => b.revisionNumber - a.revisionNumber),
    [data],
  );

  const selected = selectedRevisionId
    ? revisions.find((r) => r.id === selectedRevisionId) ?? null
    : null;
  const label = selected ? `rev ${selected.revisionNumber}` : `rev ${latestRevisionNumber}`;
  // A single-revision (or unknown) history is nothing to browse — but the
  // trigger still shows the current revision number as a passive badge.
  const hasHistory = revisions.length > 1;

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          className="flex items-center gap-0.5 rounded-sm px-1.5 py-0.5 text-2xs font-medium tabular-nums"
          style={{ border: "1px solid var(--edge)", color: "var(--midnight-muted)" }}
          aria-label={`Revision history for ${documentKey}`}
          onClick={(e) => e.stopPropagation()}
        >
          {label}
          <ChevronDown size={12} strokeWidth={1.75} />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="max-h-80 overflow-y-auto" onClick={(e) => e.stopPropagation()}>
        <DropdownMenuItem
          onSelect={() => onSelectRevision(null)}
          className="text-caption"
          data-active={selectedRevisionId === null || undefined}
        >
          Current · rev {latestRevisionNumber}
        </DropdownMenuItem>
        {revisions
          .filter((revision) => revision.revisionNumber !== latestRevisionNumber)
          .map((revision) => (
            <DropdownMenuItem
              key={revision.id}
              onSelect={() => onSelectRevision(revision.id)}
              className="text-caption"
            >
              rev {revision.revisionNumber} · {timeAgo(revision.createdAt)} · {actorOf(revision)}
            </DropdownMenuItem>
          ))}
        {hasHistory ? <DropdownMenuSeparator /> : null}
        {hasHistory ? (
          <DropdownMenuItem onSelect={onOpenDiff} className="gap-2 text-caption">
            <GitCompare size={16} />
            Compare revisions…
          </DropdownMenuItem>
        ) : null}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
