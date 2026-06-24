// apps/frontend/src/components/teams/inbox/hooks/useAgentWake.ts
"use client";

import { useCallback } from "react";
import { useTeamsApi } from "@/hooks/useTeamsApi";

/**
 * Wake an agent to (re)attempt work — backs the failed-run Retry action.
 *
 * Maps to the BFF route `POST /teams/agents/{id}/wakeup`, which forwards to
 * upstream `POST /api/agents/{id}/wakeup` (wakeAgentSchema). A retry is an
 * on-demand wake; the reason is carried so the run's provenance reads
 * "retry" rather than a bare manual ping.
 *
 * No SWR revalidation here: the wakeup spawns a fresh run asynchronously, so
 * the failed row stays in the band until its acknowledgement (the row's
 * Dismiss owns that). The caller decides how to reflect in-flight/done state.
 */
export function useAgentWake() {
  const { post } = useTeamsApi();

  const wakeAgent = useCallback(
    async (agentId: string, opts?: { reason?: string }): Promise<void> => {
      await post(`/agents/${agentId}/wakeup`, {
        source: "on_demand",
        reason: opts?.reason ?? "retry",
      });
    },
    [post],
  );

  return { wakeAgent };
}
