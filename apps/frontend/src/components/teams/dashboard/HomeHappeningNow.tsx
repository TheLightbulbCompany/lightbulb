"use client";

// Home's "Happening now": the live view of the team. One row per active run —
// who, on which task, doing what — read from the same /inbox/live-runs feed
// the Inbox and Tasks indicators use. This is the daily job the org chart was
// doing; the org chart itself moves into the Agents panel.

import {
  isRunActive,
  liveRunActivity,
  liveRunTitle,
  useLiveRuns,
  type LiveRun,
} from "@/components/teams/dashboard/hooks/useLiveRuns";
import { timeAgo } from "@/components/teams/shared/lib/timeAgo";
import type { Issue } from "@/components/teams/shared/types";
import { teamsDetailUrl, teamsPanelUrl } from "@/lib/shellNav";
import { HomeRow, HomeRowsEmpty, HomeSection } from "./HomeSection";

const LIMIT = 4;

function rowTitle(run: LiveRun, issuesById: Map<string, Issue>): string {
  const issue = run.issueId ? issuesById.get(run.issueId) : undefined;
  return issue?.title || run.nextAction?.trim() || "Working";
}

export function HomeHappeningNow({
  issuesById,
  openTasks,
}: {
  issuesById: Map<string, Issue>;
  /** `/dashboard.tasks.open`; `undefined` while that request is in flight. */
  openTasks: number | undefined;
}) {
  const { runs, isLoading, isError } = useLiveRuns();
  const active = runs.filter(isRunActive).slice(0, LIMIT);
  const tasks = teamsPanelUrl("issues");

  return (
    <HomeSection
      label="Happening now"
      href={tasks}
      linkLabel={openTasks === undefined ? "Open Tasks" : `Open Tasks · ${openTasks} open`}
    >
      {isLoading && runs.length === 0 ? (
        <HomeRowsEmpty>Looking at what&apos;s running…</HomeRowsEmpty>
      ) : isError && runs.length === 0 ? (
        <HomeRowsEmpty role="alert">Couldn&apos;t see what&apos;s running just now.</HomeRowsEmpty>
      ) : active.length === 0 ? (
        <HomeRowsEmpty>Nobody is running right now.</HomeRowsEmpty>
      ) : (
        active.map((run) => {
          const started = run.startedAt ?? run.createdAt;
          const stalled = liveRunActivity(run) === "stalled";
          return (
            <HomeRow
              key={run.id}
              who={run.agentName ?? "Agent"}
              title={rowTitle(run, issuesById)}
              sub={`${liveRunTitle(run)}${started ? ` · ${timeAgo(started)}` : ""}`}
              right={
                stalled ? (
                  <span className="text-warn">stalled</span>
                ) : run.status === "queued" ? (
                  "queued"
                ) : (
                  "running"
                )
              }
              href={run.issueId ? teamsDetailUrl("issue", run.issueId) : tasks}
            />
          );
        })
      )}
    </HomeSection>
  );
}
