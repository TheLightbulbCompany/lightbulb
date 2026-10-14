// apps/frontend/src/components/teams/cases/IssueCasesPanel.tsx

// Ported from upstream Paperclip's IssueCasesPanel (paperclip/ui/src/components/IssueCasesPanel.tsx)
// (MIT, (c) 2025 Paperclip AI).

"use client";

import Link from "next/link";
import { Badge } from "@/components/ui/badge";
import { handleShellLinkClick } from "@/lib/shellNav";
import { CaseStatusBadge } from "./CaseStatusBadge";
import { caseHref } from "./caseNav";
import { useIssueCases } from "./hooks/useCases";

/**
 * Issue-page right-rail section: the cases linked to this issue, each with its
 * link role + case status. Upstream self-gates on the experimental Cases flag;
 * here the read answers 403 while the flag is off, so "no data" covers both —
 * it renders nothing when Cases is off or no cases are linked and can be
 * dropped into the issue rail unconditionally.
 */
export function IssueCasesPanel({ issueId }: { issueId: string }) {
  const { data } = useIssueCases(issueId);
  const links = data?.cases ?? [];
  if (links.length === 0) return null;

  return (
    <section className="space-y-2">
      <h3 className="text-caption font-semibold uppercase tracking-wide text-[var(--fg-mute)]">Cases</h3>
      <div className="space-y-1">
        {links.map((link) => (
          <Link
            key={link.id}
            href={caseHref(link.case.identifier)}
            prefetch={false}
            onClick={(e) => handleShellLinkClick(e, caseHref(link.case.identifier))}
            className="flex items-center gap-2 rounded-md border border-[var(--edge)] px-2.5 py-1.5 text-ui transition-colors hover:bg-[var(--plane-raised)]"
          >
            <span className="shrink-0 font-mono text-caption text-[var(--fg-mute)]">{link.case.identifier}</span>
            <span className="min-w-0 flex-1 truncate" title={link.case.title}>{link.case.title}</span>
            <Badge variant="secondary" className="shrink-0">{link.role}</Badge>
            <CaseStatusBadge status={link.case.status} />
          </Link>
        ))}
      </div>
    </section>
  );
}
