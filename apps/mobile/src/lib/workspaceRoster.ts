import { useOrganization } from "@clerk/expo";
import { useEffect, useState } from "react";

import { fetchAgents, fetchMembers, useTeamsConfig, type CompanyMember, type TeamsAgent } from "./teamsData";

export const ROSTER_POLL_MS = 30_000;

/**
 * The workspace's agents (with Paperclip status, for rings and the starter
 * pins) and people (session owners' names). Agents re-poll while enabled;
 * people load once per enable.
 *
 * Scoped to the active org: `useTeamsConfig()` is memoized forever (same
 * bearer-fetching client across a workspace switch), so the org id has to be
 * its own effect dependency — same fix as `useRoomsPlatform`
 * (`apps/mobile/src/lib/rooms.ts`), which keys off `organization?.id` too.
 * On a switch the effect re-runs and clears both lists immediately (before
 * the new fetch lands) so a previous workspace's roster never lingers.
 *
 * ponytail: an empty poll never replaces a non-empty list, so a genuine drop to
 * zero agents shows the last roster until remount. Out-of-order responses
 * (a slow fetch from the OLD org landing after the switch) are handled for
 * free by the existing `alive` flag: it is a fresh `let` per effect run, and
 * React runs the previous run's cleanup (setting ITS `alive` false) before
 * starting the new run — so a stale response can only see a false `alive`
 * and never overwrites the new org's data. No extra request-id/generation
 * counter needed.
 */
export function useWorkspaceRoster(enabled: boolean): { agents: TeamsAgent[]; members: CompanyMember[] } {
  const config = useTeamsConfig();
  const { organization } = useOrganization();
  const orgId = organization?.id ?? null;
  const [agents, setAgents] = useState<TeamsAgent[]>([]);
  const [members, setMembers] = useState<CompanyMember[]>([]);
  useEffect(() => {
    if (!enabled) return;
    let alive = true;
    setAgents([]);
    setMembers([]);
    const loadAgents = () =>
      void fetchAgents(config).then((list) => {
        if (alive && list.length > 0) setAgents(list);
      });
    loadAgents();
    void fetchMembers(config).then((list) => {
      if (alive) setMembers(list);
    });
    const timer = setInterval(loadAgents, ROSTER_POLL_MS);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [enabled, config, orgId]);
  return { agents, members };
}
