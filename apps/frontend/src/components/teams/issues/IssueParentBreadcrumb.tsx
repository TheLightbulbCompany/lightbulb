// apps/frontend/src/components/teams/issues/IssueParentBreadcrumb.tsx
"use client";

import Link from "next/link";
import { ChevronUp } from "lucide-react";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { handleShellLinkClick, teamsDetailUrl } from "@/lib/shellNav";
import type { Issue } from "@/components/teams/shared/types";

/** Rendered only when the issue has a parent (caller guards on parentId). */
export function IssueParentBreadcrumb({ parentId }: { parentId: string }) {
  const { read } = useTeamsApi();
  const { data: parent } = read<Issue>(`/issues/${parentId}`);
  const label = parent?.title?.trim() ? parent.title : "Parent issue";
  const parentUrl = teamsDetailUrl("issue", parentId);
  return (
    <Link
      href={parentUrl}
      prefetch={false}
      onClick={(e) => handleShellLinkClick(e, parentUrl)}
      className="inline-flex items-center gap-1 text-sm hover:underline"
      style={{ color: "var(--midnight)" }}
    >
      <ChevronUp size={16} />
      {label}
    </Link>
  );
}
