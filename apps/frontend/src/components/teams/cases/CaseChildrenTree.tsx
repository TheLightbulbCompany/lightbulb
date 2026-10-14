// apps/frontend/src/components/teams/cases/CaseChildrenTree.tsx

// Ported from upstream Paperclip's CaseChildrenTree (paperclip/ui/src/components/CaseChildrenTree.tsx)
// (MIT, (c) 2025 Paperclip AI).

"use client";

import { useState } from "react";
import Link from "next/link";
import { ChevronDown } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { handleShellLinkClick } from "@/lib/shellNav";
import type { CaseSummary } from "@/components/teams/shared/types";
import { CaseCopyableToken } from "./CaseIdentifierKey";
import { CaseStatusBadge } from "./CaseStatusBadge";
import { caseHref } from "./caseNav";

type CaseRelationRow = Pick<CaseSummary, "id" | "identifier" | "title" | "caseType" | "status">;

/**
 * Children tree: the parent's direct child cases with type + status chips.
 * Display only — no rollup semantics. Renders nothing structural beyond a flat
 * list; nesting depth is intentionally one level in v1.
 */
export function CaseChildrenTree({
  cases,
  maxVisible,
}: {
  cases: CaseRelationRow[];
  maxVisible?: number;
}) {
  const [expanded, setExpanded] = useState(false);
  if (cases.length === 0) {
    return <p className="text-caption text-[var(--fg-mute)]">No child cases.</p>;
  }

  const shouldCap = maxVisible != null && cases.length > maxVisible;
  const visibleChildren = shouldCap && !expanded ? cases.slice(0, maxVisible) : cases;
  const hiddenCount = cases.length - visibleChildren.length;

  return (
    <div className="space-y-1">
      <ul className="space-y-1">
        {visibleChildren.map((child) => (
          <li key={child.id}>
            <Link
              href={caseHref(child.identifier)}
              prefetch={false}
              onClick={(e) => handleShellLinkClick(e, caseHref(child.identifier))}
              className="flex items-center gap-2 rounded-md px-2.5 py-1.5 text-ui transition-colors hover:bg-[var(--plane-raised)]"
            >
              <CaseCopyableToken
                value={child.identifier}
                label="case ID"
                className="shrink-0 font-mono text-caption text-[var(--fg-mute)]"
                containerClassName="shrink-0"
                stopPropagation
              />
              <span className="min-w-0 flex-1 truncate" title={child.title}>{child.title}</span>
              <Badge variant="secondary" className="shrink-0">{child.caseType}</Badge>
              <CaseStatusBadge status={child.status} />
            </Link>
          </li>
        ))}
      </ul>
      {hiddenCount > 0 ? (
        <Button
          type="button"
          variant="ghost"
          size="sm"
          className="h-7 gap-1 px-2 text-caption text-[var(--fg-mute)]"
          onClick={() => setExpanded(true)}
        >
          <ChevronDown className="h-3.5 w-3.5" />
          Show {hiddenCount} more
        </Button>
      ) : null}
    </div>
  );
}
