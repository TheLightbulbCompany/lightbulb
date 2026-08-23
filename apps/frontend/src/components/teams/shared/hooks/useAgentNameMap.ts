// apps/frontend/src/components/teams/shared/hooks/useAgentNameMap.ts
//
// `agentId → name` for every surface that renders an agent reference it only
// has an id for.
//
// The activity feed is the reason this exists. ActivityRow has always accepted
// an `agentNames` map, but only DashboardPanel ever built one — so the same row
// rendered "Forge invoked heartbeat for …" on the Dashboard and
// "a1b2…ef34 invoked heartbeat for …" on the Activity page, from one shared
// component reading one shared feed. Ids are not a display value: an activity
// log nobody can read is not a log.
//
// Extracted rather than duplicated because the map is derived state over a read
// six panels already make. SWR dedupes `/agents` across them, so a second
// consumer costs one cache hit, not one request.

import { useMemo } from "react";

import { useTeamsApi } from "@/hooks/useTeamsApi";
import type { CompanyAgent } from "@/components/teams/shared/types";

/** Stable identity for the empty case, so a consumer memoizing on this map
 *  does not invalidate on every render while `/agents` is still in flight. */
const EMPTY: ReadonlyMap<string, string> = new Map();

export function useAgentNameMap(): ReadonlyMap<string, string> {
  const { read } = useTeamsApi();
  const { data } = read<{ agents: CompanyAgent[] }>("/agents");

  return useMemo(() => {
    const agents = data?.agents;
    if (!agents?.length) return EMPTY;
    // Agents with a blank name would otherwise resolve to "" and render as a
    // gap where a name should be — worse than the id fallback, which at least
    // says something. Leaving them out lets the caller fall back.
    return new Map(
      agents
        .filter((a) => a.id && a.name)
        .map((a) => [a.id, a.name] as const),
    );
  }, [data]);
}
