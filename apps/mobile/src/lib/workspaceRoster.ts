import { useEffect, useState } from "react";

import { fetchAgents, fetchMembers, useTeamsConfig, type CompanyMember, type TeamsAgent } from "./teamsData";

export const ROSTER_POLL_MS = 30_000;

/**
 * The workspace's agents (with Paperclip status, for rings and the starter
 * pins) and people (session owners' names). Agents re-poll while enabled;
 * people load once per enable.
 *
 * ponytail: an empty poll never replaces a non-empty list, so a genuine drop to
 * zero agents shows the last roster until remount.
 */
export function useWorkspaceRoster(enabled: boolean): { agents: TeamsAgent[]; members: CompanyMember[] } {
  const config = useTeamsConfig();
  const [agents, setAgents] = useState<TeamsAgent[]>([]);
  const [members, setMembers] = useState<CompanyMember[]>([]);
  useEffect(() => {
    if (!enabled) return;
    let alive = true;
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
  }, [enabled, config]);
  return { agents, members };
}
