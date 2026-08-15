// apps/frontend/src/components/teams/agents/hooks/useAgentMutations.ts
"use client";

import { useCallback } from "react";
import { useSWRConfig } from "swr";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import type { CompanyAgent } from "@/components/teams/shared/types";

// All /teams reads use keys prefixed "/teams". The agents LIST key is
// "/teams/agents" (org chart + agents list); the per-agent DETAIL key is
// "/teams/agents/{id}" (AgentManageSection's status +
// reportsTo). Every mutation invalidates BOTH so the detail view doesn't show
// stale state after a change (status dots also update via events).
const AGENTS_KEY = "/teams/agents";

export function useAgentMutations() {
  const { patch, post, del } = useTeamsApi();
  const { mutate } = useSWRConfig();

  const revalidate = useCallback(
    (agentId: string) => {
      mutate(AGENTS_KEY);
      mutate(`${AGENTS_KEY}/${agentId}`);
    },
    [mutate],
  );

  const setReportsTo = useCallback(
    async (agentId: string, managerId: string | null) => {
      // null clears the manager (root). Paperclip's updateAgentSchema is
      // reportsTo: uuid().nullable() — it accepts null but rejects "" — and
      // the BFF forwards the explicit null via exclude_unset.
      await patch(`/agents/${agentId}`, { reportsTo: managerId });
      revalidate(agentId);
    },
    [patch, revalidate],
  );

  const pause = useCallback(
    async (agentId: string) => {
      await post(`/agents/${agentId}/pause`, {});
      revalidate(agentId);
    },
    [post, revalidate],
  );

  const resume = useCallback(
    async (agentId: string) => {
      await post(`/agents/${agentId}/resume`, {});
      revalidate(agentId);
    },
    [post, revalidate],
  );

  // Recovery ops for a wedged agent (A7). Neither dispatches an inference run
  // (unlike wakeup/Retry), so they're safe to fire without a spend gate.
  // `resetSession` clears the agent's runtime session state; `clearError`
  // flips an error-status agent back to idle. Both revalidate so the status
  // dot + detail view reflect the recovery immediately.
  const resetSession = useCallback(
    async (agentId: string) => {
      await post(`/agents/${agentId}/reset-session`, {});
      revalidate(agentId);
    },
    [post, revalidate],
  );

  const clearError = useCallback(
    async (agentId: string) => {
      await post(`/agents/${agentId}/clear-error`, {});
      revalidate(agentId);
    },
    [post, revalidate],
  );

  // The Teams lifecycle endpoint owns the whole transaction: preserve open
  // Paperclip work, remove OpenClaw/config/workspace state, then remove the
  // Paperclip row. Keeping this behind one backend call also works while the
  // user's container is stopped.
  const remove = useCallback(
    async (agentId: string) => {
      await del(`/agents/${agentId}`);
      mutate(
        AGENTS_KEY,
        (cur: { agents?: CompanyAgent[] } | undefined) =>
          cur?.agents ? { ...cur, agents: cur.agents.filter((a) => a.id !== agentId) } : cur,
        { revalidate: false },
      );
      mutate(`${AGENTS_KEY}/${agentId}`, undefined, { revalidate: false });
    },
    [del, mutate],
  );

  return { setReportsTo, pause, resume, resetSession, clearError, remove };
}
