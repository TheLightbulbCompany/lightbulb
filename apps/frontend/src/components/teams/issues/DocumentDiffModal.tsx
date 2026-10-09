"use client";

// Ported from upstream Paperclip `ui/src/components/DocumentDiffModal.tsx` (A3),
// re-fetched via useTeamsApi/SWR instead of react-query.

import { useMemo, useState } from "react";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { timeAgo } from "@/components/teams/shared/lib/timeAgo";
import { buildLineDiff, type DiffRow } from "./lib/lineDiff";
import type { DocumentRevision } from "./lib/documentReviewTypes";

function revisionLabel(revision: DocumentRevision): string {
  const actor = revision.createdByUserId ? "board" : revision.createdByAgentId ? "agent" : "system";
  return `rev ${revision.revisionNumber} · ${timeAgo(revision.createdAt)} · ${actor}`;
}

export function DocumentDiffModal({
  issueId,
  basePath = `/issues/${issueId}`,
  documentKey,
  latestRevisionNumber,
  open,
  onOpenChange,
}: {
  issueId: string;
  /** Owner of the document routes; a case passes `/cases/{id}`. */
  basePath?: string;
  documentKey: string;
  latestRevisionNumber: number;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const { read } = useTeamsApi();
  const { data } = read<{ revisions: DocumentRevision[] }>(
    open ? `${basePath}/documents/${documentKey}/revisions` : null,
  );

  const sortedRevisions = useMemo(
    () => [...(data?.revisions ?? [])].sort((a, b) => b.revisionNumber - a.revisionNumber),
    [data],
  );

  const [leftRevisionId, setLeftRevisionId] = useState<string | null>(null);
  const [rightRevisionId, setRightRevisionId] = useState<string | null>(null);

  const effectiveLeftId =
    leftRevisionId
    ?? sortedRevisions.find((r) => r.revisionNumber === latestRevisionNumber - 1)?.id
    ?? sortedRevisions[sortedRevisions.length - 1]?.id
    ?? null;
  const effectiveRightId =
    rightRevisionId
    ?? sortedRevisions.find((r) => r.revisionNumber === latestRevisionNumber)?.id
    ?? sortedRevisions[0]?.id
    ?? null;

  const leftRevision = sortedRevisions.find((r) => r.id === effectiveLeftId) ?? null;
  const rightRevision = sortedRevisions.find((r) => r.id === effectiveRightId) ?? null;
  const diffRows = useMemo(
    () => buildLineDiff(leftRevision?.body ?? "", rightRevision?.body ?? ""),
    [leftRevision, rightRevision],
  );

  const rowClass: Record<DiffRow["kind"], string> = {
    context: "",
    removed: "bg-err/10 text-err",
    added: "bg-ok/10 text-ok",
    truncated: "",
  };
  const marker: Record<DiffRow["kind"], string> = {
    context: " ",
    removed: "-",
    added: "+",
    truncated: " ",
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="flex max-h-[85vh] w-full max-w-4xl flex-col overflow-hidden">
        <DialogHeader>
          <DialogTitle>
            Diff — <span className="font-mono text-sm">{documentKey}</span>
          </DialogTitle>
        </DialogHeader>

        <div className="flex flex-wrap items-center gap-4">
          <div className="flex items-center gap-2">
            <span className="label-caps text-err">Old</span>
            <Select value={effectiveLeftId ?? ""} onValueChange={setLeftRevisionId}>
              <SelectTrigger aria-label="Old revision" className="h-7 w-56 text-xs">
                <SelectValue placeholder="Select revision" />
              </SelectTrigger>
              <SelectContent>
                {sortedRevisions.map((revision) => (
                  <SelectItem key={revision.id} value={revision.id} className="text-xs">
                    {revisionLabel(revision)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="flex items-center gap-2">
            <span className="label-caps text-ok">New</span>
            <Select value={effectiveRightId ?? ""} onValueChange={setRightRevisionId}>
              <SelectTrigger aria-label="New revision" className="h-7 w-56 text-xs">
                <SelectValue placeholder="Select revision" />
              </SelectTrigger>
              <SelectContent>
                {sortedRevisions.map((revision) => (
                  <SelectItem key={revision.id} value={revision.id} className="text-xs">
                    {revisionLabel(revision)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        </div>

        <div
          className="flex-1 overflow-auto rounded-md text-xs"
          style={{ border: "1px solid var(--edge)" }}
        >
          {!data ? (
            <div className="p-6 text-center text-sm" style={{ color: "var(--midnight-muted)" }}>
              Loading revisions…
            </div>
          ) : !leftRevision || !rightRevision ? (
            <div className="p-6 text-center text-sm" style={{ color: "var(--midnight-muted)" }}>
              Select two revisions to compare.
            </div>
          ) : leftRevision.id === rightRevision.id ? (
            <div className="p-6 text-center text-sm" style={{ color: "var(--midnight-muted)" }}>
              Both sides are the same revision.
            </div>
          ) : (
            <div className="font-mono leading-6">
              {diffRows.map((row, index) =>
                row.kind === "truncated" ? (
                  <div
                    key={`truncated-${index}`}
                    data-testid="document-diff-truncated"
                    className="px-4 py-3 text-center text-xs"
                    style={{ color: "var(--midnight-muted)", borderTop: "1px solid var(--edge)" }}
                  >
                    {row.text}
                  </div>
                ) : (
                  <div
                    key={`${row.kind}-${index}-${row.oldLineNumber ?? "x"}-${row.newLineNumber ?? "x"}`}
                    data-testid="document-diff-row"
                    className={`grid grid-cols-[3rem_3rem_1rem_1fr] px-2 ${rowClass[row.kind]}`}
                  >
                    <span className="select-none pr-2 text-right" style={{ color: "var(--midnight-muted)" }}>
                      {row.oldLineNumber ?? ""}
                    </span>
                    <span className="select-none pr-2 text-right" style={{ color: "var(--midnight-muted)" }}>
                      {row.newLineNumber ?? ""}
                    </span>
                    <span className="select-none text-center" style={{ color: "var(--midnight-muted)" }}>
                      {marker[row.kind]}
                    </span>
                    <pre className="overflow-x-auto whitespace-pre-wrap break-words px-2 text-inherit">
                      {row.text.length > 0 ? row.text : " "}
                    </pre>
                  </div>
                ),
              )}
            </div>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}
