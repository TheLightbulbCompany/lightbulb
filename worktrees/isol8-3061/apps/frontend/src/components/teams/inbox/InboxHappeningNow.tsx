"use client";

// Inbox section 2 — the live view of the team, same feed and same three words
// as Home's "Happening now" (dashboard/HomeHappeningNow.tsx). `stalled` is
// checked BEFORE `queued`: a queued run that has gone silent is stalled, and
// saying "queued" there would be a comfortable lie.

import {
  isRunActive,
  liveRunActivity,
  liveRunTitle,
  useLiveRuns,
} from "@/components/teams/dashboard/hooks/useLiveRuns";
import { PanelBand, PanelBandEmpty } from "@/components/teams/shell";
import { timeAgo } from "@/components/teams/shared/lib/timeAgo";
import { handleShellLinkClick, teamsDetailUrl } from "@/lib/shellNav";

const LIMIT = 6;

export function InboxHappeningNow() {
  const { runs, isLoading, isError } = useLiveRuns();
  const active = runs.filter(isRunActive).slice(0, LIMIT);

  return (
    <PanelBand
      id="inbox-happening-now"
      label="Happening now"
      right={active.length > 0 ? <span>{active.length} running</span> : undefined}
    >
      {isLoading && runs.length === 0 ? (
        <PanelBandEmpty>Looking at what&apos;s running…</PanelBandEmpty>
      ) : isError && runs.length === 0 ? (
        <PanelBandEmpty role="alert">Couldn&apos;t see what&apos;s running just now.</PanelBandEmpty>
      ) : active.length === 0 ? (
        <PanelBandEmpty>Nobody is running right now.</PanelBandEmpty>
      ) : (
        <ul className="flex flex-col">
          {active.map((run) => {
            const started = run.startedAt ?? run.createdAt;
            const stalled = liveRunActivity(run) === "stalled";
            const href = run.issueId ? teamsDetailUrl("issue", run.issueId) : null;
            const body = (
              <>
                <span className="min-w-0 flex-1 truncate text-[var(--midnight)]">
                  {run.nextAction?.trim() || "Working"}
                </span>
                <span className="shrink-0 text-xs text-[var(--midnight-muted)]">
                  {run.agentName ?? "Agent"}
                  {started ? ` · ${timeAgo(started)}` : ""}
                </span>
                <span className="w-16 shrink-0 text-right text-xs">
                  {stalled ? (
                    <span className="text-warn">stalled</span>
                  ) : run.status === "queued" ? (
                    "queued"
                  ) : (
                    "running"
                  )}
                </span>
              </>
            );
            return (
              <li key={run.id} className="border-b border-[var(--edge-soft)] last:border-b-0">
                {href ? (
                  <a
                    href={href}
                    onClick={(e) => handleShellLinkClick(e, href)}
                    title={liveRunTitle(run)}
                    className="flex items-baseline gap-3 px-2 py-2 hover:bg-[var(--edge-soft)]"
                  >
                    {body}
                  </a>
                ) : (
                  <div title={liveRunTitle(run)} className="flex items-baseline gap-3 px-2 py-2">
                    {body}
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </PanelBand>
  );
}
