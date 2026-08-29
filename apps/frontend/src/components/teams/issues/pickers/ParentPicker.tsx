"use client";

import { useMemo, useState } from "react";
import Link from "next/link";
import { Check } from "lucide-react";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { StatusIcon } from "@/components/teams/shared/components/StatusIcon";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { cn } from "@/lib/utils";
import { handleShellLinkClick, teamsDetailUrl } from "@/lib/shellNav";
import type { Issue } from "@/components/teams/shared/types";

export interface ParentPickerProps {
  /** The issue whose parent is being edited. */
  issueId: string;
  /** Current parent id (null when this is a top-level task). */
  parentId: string | null;
  /** Reparent to ``parentId`` or ``null`` to clear. */
  onChange: (parentId: string | null) => void;
  className?: string;
}

/** Editable Parent rail control: a themed popover of candidate issues plus a
 * "Clear parent" option. The current parent is shown as an in-shell link when
 * the popover is closed (identifier + title, matching the old read-only row).
 *
 * Cycle prevention is intentionally shallow — we only exclude the issue itself
 * from the candidate list, which blocks the trivial self-parent cycle. A full
 * descendant exclusion would need the subtree, which the rail doesn't load; the
 * server is the authority on deeper cycles (it rejects a parent that would
 * create one), so the picker stays cheap and lets the BFF be the backstop. */
export function ParentPicker({
  issueId,
  parentId,
  onChange,
  className,
}: ParentPickerProps) {
  const { read } = useTeamsApi();
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");

  // Candidate parents come from the company issue list (same source the
  // blocker picker uses). Filtered client-side — there's no server-side
  // search on the Teams BFF.
  const { data: issuesResp } = read<{ issues: Issue[] }>("/issues");
  const allIssues = issuesResp?.issues;

  // Resolve the current parent for the closed-state link. The list read above
  // usually contains it, so this avoids a second per-parent fetch.
  const parent = useMemo(
    () => (parentId ? allIssues?.find((i) => i.id === parentId) ?? null : null),
    [allIssues, parentId],
  );

  const candidates = useMemo(() => {
    const q = query.trim().toLowerCase();
    return (allIssues ?? [])
      .filter((candidate) => candidate.id !== issueId)
      .filter((candidate) => {
        if (!q) return true;
        const label = `${candidate.identifier ?? ""} ${candidate.title}`.toLowerCase();
        return label.includes(q);
      })
      .sort((a, b) => {
        const al = `${a.identifier ?? ""} ${a.title}`.trim();
        const bl = `${b.identifier ?? ""} ${b.title}`.trim();
        return al.localeCompare(bl);
      });
  }, [allIssues, issueId, query]);

  const select = (nextParentId: string | null) => {
    onChange(nextParentId);
    setOpen(false);
    setQuery("");
  };

  const parentLabel = parent
    ? (parent.identifier ?? parent.id.slice(0, 8))
    : parentId
      ? parentId.slice(0, 8)
      : null;
  const parentTitle = parent?.title?.trim() ?? "";
  const parentUrl = parentId ? teamsDetailUrl("issue", parentId) : null;

  return (
    <span className={cn("inline-flex min-w-0 items-baseline gap-2", className)}>
      {parentUrl && (
        <Link
          href={parentUrl}
          prefetch={false}
          onClick={(e) => handleShellLinkClick(e, parentUrl)}
          className="inline-flex min-w-0 items-baseline gap-1.5 hover:underline"
          style={{ color: "var(--midnight)" }}
        >
          <span className="font-mono text-xs shrink-0">{parentLabel}</span>
          {parentTitle && <span className="truncate">{parentTitle}</span>}
        </Link>
      )}
      <Popover
        open={open}
        onOpenChange={(next) => {
          setOpen(next);
          if (!next) setQuery("");
        }}
      >
        <PopoverTrigger asChild>
          <button
            type="button"
            className="text-xs hover:underline shrink-0"
            style={{ color: "var(--midnight-muted)" }}
          >
            {parentId ? "Change" : "Set parent"}
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
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search issues..."
            className="w-full px-2 py-1.5 text-xs bg-transparent mb-1 rounded-sm"
            style={{ borderBottom: "1px solid var(--edge)" }}
            aria-label="Search issues to set as parent"
          />
          <ul className="max-h-48 overflow-y-auto" role="listbox">
            <li role="option" aria-selected={parentId === null}>
              <button
                type="button"
                onClick={() => select(null)}
                className="w-full text-left px-2 py-1.5 text-sm rounded-sm hover:bg-[var(--cream-deep)]"
                style={{ color: "var(--midnight-muted)" }}
              >
                Clear parent
              </button>
            </li>
            {candidates.map((candidate) => {
              const isSelected = parentId === candidate.id;
              return (
                <li key={candidate.id} role="option" aria-selected={isSelected}>
                  <button
                    type="button"
                    onClick={() => select(candidate.id)}
                    className={cn(
                      "flex w-full items-center gap-2 px-2 py-1.5 text-left text-sm rounded-sm hover:bg-[var(--cream-deep)]",
                      isSelected && "bg-[var(--cream-deeper)]",
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
                    {isSelected && (
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
  );
}
