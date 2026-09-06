"use client";

// Home's "Recent work": the six newest deliverables from the Library, each
// linked to the task that produced it. Same route the Library reads
// (ArtifactsPanel RECENT_PATH) with a smaller limit; a separate SWR key, so
// opening Home does not pull the Library's hundred-row page.
//
// A failed read is never rendered as rows (ArtifactsPanel's rule): with SWR's
// retained data an error on revalidation would otherwise list yesterday's
// deliverables as if they were fresh.

import { useTeamsApi } from "@/hooks/useTeamsApi";
import { timeAgo } from "@/components/teams/shared/lib/timeAgo";
import type { CompanyArtifactsResponse } from "@/components/teams/shared/types";
import { teamsDetailUrl, teamsPanelUrl } from "@/lib/shellNav";
import { HomeRow, HomeRowsEmpty, HomeSection } from "./HomeSection";

export const RECENT_WORK_PATH = "/artifacts?groupBy=none&limit=6";

export function HomeRecentWork() {
  const { read } = useTeamsApi();
  const q = read<CompanyArtifactsResponse>(RECENT_WORK_PATH);
  const artifacts = q.error ? [] : (q.data?.artifacts ?? []);

  return (
    <HomeSection label="Recent work" href={teamsPanelUrl("artifacts")} linkLabel="Open Library">
      {q.isLoading && !q.data ? (
        <HomeRowsEmpty>Gathering recent work…</HomeRowsEmpty>
      ) : q.error ? (
        <HomeRowsEmpty role="alert">Couldn&apos;t read the Library just now.</HomeRowsEmpty>
      ) : artifacts.length === 0 ? (
        <HomeRowsEmpty>Nothing made yet. The first deliverable your agents finish lands here.</HomeRowsEmpty>
      ) : (
        artifacts.map((a) => (
          <HomeRow
            key={a.id}
            who={a.createdByAgent?.name ?? "Your team"}
            title={a.title}
            sub={[a.createdByAgent?.name, a.issue.title].filter(Boolean).join(" · ")}
            right={timeAgo(a.updatedAt)}
            href={teamsDetailUrl("issue", a.issue.id)}
          />
        ))
      )}
    </HomeSection>
  );
}
