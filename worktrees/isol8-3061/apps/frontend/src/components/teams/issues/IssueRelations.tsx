// apps/frontend/src/components/teams/issues/IssueRelations.tsx

// Ported from upstream Paperclip's IssueProperties.tsx relations block
// (paperclip/ui/src/components/IssueProperties.tsx — `RemovableIssueReferencePill`,
// the "Blocked by" PropertyRow + picker, and the read-only "Blocking" row)
// (MIT, © 2025 Paperclip AI). Retheme-mapped onto the editorial palette and
// re-wired to the Teams BFF: candidates come from `GET /teams/issues` and the
// add/remove handlers send the FULL desired `blockedByIssueIds` array (replace
// semantics), exactly like upstream's `toggleBlockedBy` / `removeBlockedBy`.

"use client";

import { useMemo, useState } from "react";
import Link from "next/link";
import { Check, Plus, X } from "lucide-react";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { EditorialButton, EditorialDialog } from "@/components/teams/shell";
import { StatusIcon } from "@/components/teams/shared/components/StatusIcon";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { cn } from "@/lib/utils";
import { handleShellLinkClick, teamsDetailUrl } from "@/lib/shellNav";
import type { Issue, IssueRelationIssueSummary } from "@/components/teams/shared/types";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";

/** A removable "Blocked by" chip: links to the blocker, with a hover X that
 * opens a confirm dialog before removing (mirrors upstream's
 * `RemovableIssueReferencePill`). */
function RemovableRelationChip({
  issue,
  onRemove,
}: {
  issue: IssueRelationIssueSummary;
  onRemove: (issueId: string) => void;
}) {
  const [confirmOpen, setConfirmOpen] = useState(false);
  const issueUrl = teamsDetailUrl("issue", issue.id);
  const issueLabel = issue.identifier ?? issue.title;
  const confirmLabel = issue.identifier
    ? `${issue.identifier}: ${issue.title}`
    : issue.title;
  const removeLabel = `Remove ${issueLabel} as blocker`;

  const content = (
    <>
      <StatusIcon status={issue.status} className="h-3 w-3 shrink-0" />
      <span className="truncate">{issueLabel}</span>
    </>
  );

  return (
    <>
      <span
        className="group inline-flex items-center gap-1 rounded-full py-0.5 pl-1 pr-2 text-xs"
        style={{
          border: "1px solid var(--edge)",
          background: "var(--cream-deep)",
          color: "var(--midnight-soft)",
        }}
        title={issue.title}
        aria-label={`Issue ${issueLabel}: ${issue.title}`}
      >
        <Tooltip>
          <TooltipTrigger asChild>
            <button
              type="button"
              className="inline-flex h-4 w-4 shrink-0 items-center justify-center rounded-full opacity-0 transition-opacity duration-(--dur-fast) ease-out group-hover:opacity-100 group-focus-within:opacity-100 focus-visible:opacity-100"
              style={{ color: "var(--midnight-muted)" }}
              aria-label={removeLabel}
              onClick={(e) => {
                e.preventDefault();
                e.stopPropagation();
                setConfirmOpen(true);
              }}
            >
              <X size={12} strokeWidth={1.75} />
            </button>
          </TooltipTrigger>
          <TooltipContent>{removeLabel}</TooltipContent>
        </Tooltip>
        {issue.identifier ? (
          <Link
            href={issueUrl}
            prefetch={false}
            onClick={(e) => handleShellLinkClick(e, issueUrl)}
            className="inline-flex min-w-0 items-center gap-1 no-underline hover:underline"
            aria-label={`Issue ${issueLabel}: ${issue.title}`}
          >
            {content}
          </Link>
        ) : (
          <span className="inline-flex min-w-0 items-center gap-1">{content}</span>
        )}
      </span>
      <EditorialDialog
        open={confirmOpen}
        onOpenChange={setConfirmOpen}
        title="Remove blocker?"
        description={`Remove ${confirmLabel} as a blocker for this issue.`}
        footer={
          <>
            <EditorialButton
              variant="secondary"
              size="sm"
              onClick={() => setConfirmOpen(false)}
            >
              Cancel
            </EditorialButton>
            <EditorialButton
              variant="destructive"
              size="sm"
              onClick={() => {
                onRemove(issue.id);
                setConfirmOpen(false);
              }}
            >
              Remove blocker
            </EditorialButton>
          </>
        }
      />
    </>
  );
}

/** A read-only "Blocking" chip linking to the blocked issue. */
function RelationChip({ issue }: { issue: IssueRelationIssueSummary }) {
  const issueUrl = teamsDetailUrl("issue", issue.id);
  const issueLabel = issue.identifier ?? issue.title;
  const content = (
    <>
      <StatusIcon status={issue.status} className="h-3 w-3 shrink-0" />
      <span className="truncate">{issueLabel}</span>
    </>
  );
  return (
    <Link
      href={issueUrl}
      prefetch={false}
      onClick={(e) => handleShellLinkClick(e, issueUrl)}
      className="inline-flex min-w-0 items-center gap-1 rounded-full px-2 py-0.5 text-xs no-underline hover:underline"
      style={{
        border: "1px solid var(--edge)",
        background: "var(--cream-deep)",
        color: "var(--midnight-soft)",
      }}
      title={issue.title}
      aria-label={`Issue ${issueLabel}: ${issue.title}`}
    >
      {content}
    </Link>
  );
}

export interface IssueRelationsProps {
  issue: Issue;
  /** Receives the FULL desired blocker-id set (replace semantics). */
  onBlockedByChange: (ids: string[]) => void;
}

export function IssueRelations({ issue, onBlockedByChange }: IssueRelationsProps) {
  const { read } = useTeamsApi();
  const [pickerOpen, setPickerOpen] = useState(false);
  const [search, setSearch] = useState("");

  // Candidate blockers come from the company issue list. There's no server-side
  // search endpoint on the Teams BFF, so we filter the full list client-side.
  const { data: issuesResp } = read<{ issues: Issue[] }>("/issues");
  const allIssues = issuesResp?.issues;

  const blockedBy = issue.blockedBy ?? [];
  const blocks = issue.blocks ?? [];
  const blockedByIds = useMemo(
    () => (issue.blockedBy ?? []).map((relation) => relation.id),
    [issue.blockedBy],
  );

  // Candidates: every other issue (an issue can't block itself), filtered by
  // the search box. Selected blockers stay in the list (toggle-off support).
  const candidates = useMemo(() => {
    const query = search.trim().toLowerCase();
    return (allIssues ?? [])
      .filter((candidate) => candidate.id !== issue.id)
      .filter((candidate) => {
        if (!query) return true;
        const label = `${candidate.identifier ?? ""} ${candidate.title}`.toLowerCase();
        return label.includes(query);
      })
      .sort((a, b) => {
        const al = `${a.identifier ?? ""} ${a.title}`.trim();
        const bl = `${b.identifier ?? ""} ${b.title}`.trim();
        return al.localeCompare(bl);
      });
  }, [allIssues, issue.id, search]);

  // Full-array replace, exactly like upstream's toggleBlockedBy/removeBlockedBy.
  const toggle = (candidateId: string) => {
    const next = blockedByIds.includes(candidateId)
      ? blockedByIds.filter((id) => id !== candidateId)
      : [...blockedByIds, candidateId];
    onBlockedByChange(next);
    setPickerOpen(false);
    setSearch("");
  };
  const remove = (candidateId: string) => {
    onBlockedByChange(blockedByIds.filter((id) => id !== candidateId));
  };

  return (
    <>
      <div
        className="grid grid-cols-[100px_1fr] gap-3 items-baseline py-2 text-sm"
        style={{ color: "var(--midnight-soft)", borderBottom: "1px solid var(--edge)" }}
      >
        <dt className="section-label" style={{ marginTop: 0, marginBottom: 0 }}>
          <b>Blocked by</b>
        </dt>
        <dd className="min-w-0">
          <span className="flex flex-wrap items-center gap-1">
            {blockedBy.map((relation) => (
              <RemovableRelationChip
                key={relation.id}
                issue={relation}
                onRemove={remove}
              />
            ))}
            <Popover
              open={pickerOpen}
              onOpenChange={(open) => {
                setPickerOpen(open);
                if (!open) setSearch("");
              }}
            >
              <PopoverTrigger asChild>
                <button
                  type="button"
                  className="inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs transition-colors hover:underline"
                  style={{ border: "1px solid var(--edge)", color: "var(--midnight-muted)" }}
                >
                  <Plus size={12} strokeWidth={1.75} />
                  Add blocker
                </button>
              </PopoverTrigger>
              <PopoverContent
                align="start"
                className="w-72 p-1"
                style={{
                  background: "var(--cream)",
                  border: "1px solid var(--edge)",
                  color: "var(--midnight)",
                }}
              >
                <input
                  autoFocus
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                  placeholder="Search issues..."
                  className="w-full px-2 py-1.5 text-xs bg-transparent mb-1 rounded-sm"
                  style={{ borderBottom: "1px solid var(--edge)" }}
                  aria-label="Search issues to add as blockers"
                />
                <ul className="max-h-48 overflow-y-auto" role="listbox">
                  {candidates.map((candidate) => {
                    const selected = blockedByIds.includes(candidate.id);
                    return (
                      <li
                        key={candidate.id}
                        role="option"
                        aria-selected={selected}
                      >
                        <button
                          type="button"
                          onClick={() => toggle(candidate.id)}
                          className={cn(
                            "flex w-full items-center gap-2 px-2 py-1.5 text-left text-sm rounded-sm hover:bg-[var(--cream-deep)]",
                            selected && "bg-[var(--cream-deeper)]",
                          )}
                        >
                          <StatusIcon
                            status={candidate.status}
                            className="h-3 w-3 shrink-0"
                          />
                          <span className="truncate">
                            {candidate.identifier ? `${candidate.identifier} ` : ""}
                            {candidate.title}
                          </span>
                          {selected && (
                            <Check size={16}
                              className="ml-auto shrink-0"
                              aria-hidden="true"
                            />
                          )}
                        </button>
                      </li>
                    );
                  })}
                  {candidates.length === 0 && (
                    <li className="px-2 py-2 text-xs" style={{ color: "var(--midnight-muted)" }}>
                      No matching issues.
                    </li>
                  )}
                </ul>
              </PopoverContent>
            </Popover>
          </span>
        </dd>
      </div>

      <div
        className="grid grid-cols-[100px_1fr] gap-3 items-baseline py-2 text-sm"
        style={{ color: "var(--midnight-soft)", borderBottom: "1px solid var(--edge)" }}
      >
        <dt className="section-label" style={{ marginTop: 0, marginBottom: 0 }}>
          <b>Blocking</b>
        </dt>
        <dd className="min-w-0">
          {blocks.length > 0 ? (
            <span className="flex flex-wrap gap-1">
              {blocks.map((relation) => (
                <RelationChip key={relation.id} issue={relation} />
              ))}
            </span>
          ) : (
            <span data-empty="true" style={{ color: "var(--midnight-muted)" }}>
              —
            </span>
          )}
        </dd>
      </div>
    </>
  );
}
