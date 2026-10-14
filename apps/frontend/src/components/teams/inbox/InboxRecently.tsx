"use client";

// Inbox section 3 — what happened, day by day. This is the old Activity page,
// which is now a band rather than its own tab.
//
// It passes `agentNames` to ActivityRow, which the Activity page never did:
// the row has been able to resolve agent actors to roster names since June, but
// its only caller never wired the map, so every actor rendered as a raw id.

import { useMemo, useState } from "react";
import { ActivityRow } from "@/components/teams/dashboard/ActivityRow";
import { useActivityFeed } from "@/components/teams/dashboard/hooks/useActivityFeed";
import { groupByDay } from "@/components/teams/inbox/lib/groupByDay";
import { PanelBand, PanelBandEmpty } from "@/components/teams/shell";
import type { CompanyAgent } from "@/components/teams/shared/types";

// Matches the old Activity page's cap. The feed is a capped list, never a
// count of everything that ever happened.
const LIMIT = 200;

export function InboxRecently({ agents }: { agents: CompanyAgent[] }) {
  const { events, isLoading, isError } = useActivityFeed(LIMIT);
  const [filter, setFilter] = useState("all");

  const agentNames = useMemo(() => new Map(agents.map((a) => [a.id, a.name])), [agents]);

  const entityTypes = useMemo(
    () =>
      Array.from(new Set(events.map((e) => e.entityType).filter((t): t is string => !!t))).sort(),
    [events],
  );

  const filtered = useMemo(
    () => (filter === "all" ? events : events.filter((e) => e.entityType === filter)),
    [events, filter],
  );

  const days = useMemo(() => groupByDay(filtered, (e) => e.createdAt), [filtered]);

  return (
    <PanelBand
      id="inbox-recently"
      label="Recently"
      right={
        entityTypes.length > 0 ? (
          <select
            aria-label="Filter by type"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            className="h-7 rounded-sm border border-[var(--edge)] bg-[var(--cream)] px-2 text-caption text-[var(--midnight)]"
          >
            <option value="all">All types</option>
            {entityTypes.map((type) => (
              <option key={type} value={type}>
                {type.charAt(0).toUpperCase() + type.slice(1)}
              </option>
            ))}
          </select>
        ) : undefined
      }
    >
      {isLoading && events.length === 0 ? (
        <PanelBandEmpty>Looking at what happened…</PanelBandEmpty>
      ) : isError && events.length === 0 ? (
        <PanelBandEmpty role="alert">Couldn&apos;t load what happened.</PanelBandEmpty>
      ) : events.length === 0 ? (
        // Only when the FEED is empty. A filter that hides every row leaves no
        // rows and no line: "nothing has happened yet" would be false.
        <PanelBandEmpty>Nothing has happened yet.</PanelBandEmpty>
      ) : (
        <div className="flex flex-col gap-3">
          {days.map((day) => (
            <div key={day.label} className="flex flex-col">
              <p className="label-caps sticky top-0 bg-[var(--cream)] py-1 text-[var(--midnight-muted)]">
                {day.label}
              </p>
              {day.items.map((evt) => (
                <div key={evt.id} className="border-b border-[var(--edge-soft)] last:border-b-0">
                  <ActivityRow event={evt} agentNames={agentNames} />
                </div>
              ))}
            </div>
          ))}
        </div>
      )}
    </PanelBand>
  );
}
