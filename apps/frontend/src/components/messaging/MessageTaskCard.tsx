"use client";

// Tasks an agent names in a room message (`ISO-14`), shown live under the
// text. No new data: one shared read of the Tasks panel's own list key, so any
// number of messages costs zero extra requests, and freshness is that key's
// SWR refresh. Unknown ids, no Paperclip company, or a failed read → nothing.

import { useMemo } from "react";
import { DOCUMENT_LINK_RE } from "@lightbulb/shared";
import { linkedIssueIdentifiers } from "./appLinks";
import { useInlineTeamsNav } from "@/components/chat/InlineTeamsNav";
import { useLiveRuns } from "@/components/teams/dashboard/hooks/useLiveRuns";
import { IssueRow } from "@/components/teams/inbox/IssueRow";
import { buildLiveIssueState } from "@/components/teams/shared/lib/liveRuns";
import type { CompanyAgent, Issue } from "@/components/teams/shared/types";
import { EditorialButton } from "@/components/teams/shell";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { navigateShell, teamsDetailUrl } from "@/lib/shellNav";

// Paperclip's identifier is `${company.issuePrefix}-${number}`
// (packages/shared/src/issue-references.ts ISSUE_REFERENCE_IDENTIFIER_RE).
// Uppercase-only here, unlike upstream: in chat prose `utf-8` is not a task.
const CODE = /```[\s\S]*?```|`[^`\n]*`/g;
const TOKEN = /https?:\/\/\S+|\b[A-Z][A-Z0-9]*-\d+\b/g;
// Same key as IssuesPanel, so the cache is shared with the Tasks panel.
// ponytail: the list is limit-capped upstream, so a very old task can miss; add
// an id lookup read if that bites.
const ISSUES_KEY = "/issues?excludeRoutineExecutions=true";

// `prose` reads bare ids (`ISO-14`) too — agent messages only, since a human's
// prose names things like `COVID-19`. Links to our own issues count for everyone.
export function taskIdentifiers(text: string, prose = true): string[] {
  const ids = new Set<string>();
  // A document link already renders as a document card naming its task.
  const stripped = text.replace(CODE, " ").replace(DOCUMENT_LINK_RE, " ");
  if (prose) for (const [token] of stripped.matchAll(TOKEN)) {
    if (!token.startsWith("http")) ids.add(token);
  }
  for (const id of linkedIssueIdentifiers(stripped)) ids.add(id);
  return [...ids];
}

// The one thing a task waits on the viewer for, if any.
export function needsYouAction(issue: Issue): "Review" | "Unblock" | null {
  if (issue.status === "in_review" || issue.status === "review" || issue.status === "pending") return "Review";
  const attention = typeof issue.blockerAttention === "object" ? issue.blockerAttention?.state : null;
  if (issue.status === "blocked" || attention === "needs_attention" || attention === "stalled") return "Unblock";
  return null;
}

// The named ids that are real tasks, in the order named. No ids → no read.
export function useReferencedTasks(ids: readonly string[]): Issue[] {
  const { read } = useTeamsApi();
  const { data } = read<{ issues: Issue[] }>(ids.length ? ISSUES_KEY : null);
  return useMemo(() => {
    const byIdentifier = new Map((data?.issues ?? []).map((i) => [i.identifier, i]));
    return ids.map((id) => byIdentifier.get(id)).filter((i): i is Issue => !!i);
  }, [data, ids]);
}

// IssueRow + live dot + assignee + the one needs-you action, per task.
export function TaskRowList({ issues, label }: { issues: readonly Issue[]; label?: string }) {
  const { read } = useTeamsApi();
  const { data: agentsData } = read<{ agents: CompanyAgent[] }>("/agents");
  const { runs } = useLiveRuns();
  const nav = useInlineTeamsNav();
  const live = useMemo(() => buildLiveIssueState(runs), [runs]);
  const open = (issueId: string) =>
    nav ? nav.openDetail("issue-detail", issueId) : navigateShell(teamsDetailUrl("issue", issueId));
  const assignee = (issue: Issue) =>
    agentsData?.agents.find((a) => a.id === issue.assigneeAgentId)?.name ?? undefined;

  return (
    <ul aria-label={label}>
      {issues.map((issue) => {
        const action = needsYouAction(issue);
        return (
          <li key={issue.id} className="flex items-center gap-2 border-b border-[var(--edge)] last:border-b-0">
            <IssueRow issue={issue} live={live.get(issue.id)} className="min-w-0 flex-1 border-b-0"
              trailingMeta={assignee(issue)} mobileMeta={assignee(issue)} />
            {action && (
              <EditorialButton variant="secondary" size="sm" className="mr-2 shrink-0"
                aria-label={`${action} ${issue.identifier}`} onClick={() => open(issue.id)}>
                {action}
              </EditorialButton>
            )}
          </li>
        );
      })}
    </ul>
  );
}
