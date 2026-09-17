"use client";

import { useCallback, useRef, useState } from "react";
import { useLiveEvents } from "@/hooks/useLiveEvents";
import { useRuntimeRpc } from "@/hooks/useRuntimeRpc";
import { useProvisioningState } from "@/hooks/useProvisioningState";
import { useApi } from "@/lib/api";

export type HealthState =
  | "HEALTHY"
  | "PARKED"
  | "STARTING"
  | "RECOVERING"
  | "GATEWAY_BUSY"
  | "GATEWAY_DOWN_DEAD"
  | "CONTAINER_DOWN";

interface HealthData {
  ok?: boolean;
}

interface RecoverResponse {
  action: "reprovision" | "gateway_restart" | "none" | "already_recovering";
  state: string;
  reason: string;
}

export interface SystemHealth {
  state: HealthState;
  reason: string;
  canRecover: boolean;
  actionLabel: string | null;
  recover: () => Promise<RecoverResponse | null>;
  isRecovering: boolean;
}

const HEALTH_POLL_STEADY = 60_000;
const HEALTH_POLL_DEGRADED = 5_000;

export function useSystemHealth(): SystemHealth {
  const { liveConnected } = useLiveEvents();
  const [isRecovering, setIsRecovering] = useState(false);
  const isRecoveringRef = useRef(false);
  const api = useApi();
  const { container } = useProvisioningState();
  // Never probe an AgentCore workspace (M6 task 10, row 6). The backend composer makes the
  // same call for the same reason -- "never probe a running AgentCore row" in
  // readiness_state.compute_status: AgentCore parks an idle microVM invisibly and wakes it on
  // ANY invoke, so this 60s poll would hold a box awake (and billed) for a dot whose answer
  // `runtime_phase` already gives unconditionally. `container.runtime_host` rather than the
  // shared `useRuntimeHost()` subscription, because this hook already HOLDS the status body:
  // it gets the host in the same render as `lifecycle`, with no unknown-on-first-render window
  // in which SWR would fire exactly the one invoke we are avoiding. A null method nulls the
  // SWR key, so nothing is fetched at all.
  const healthMethod =
    container && container.runtime_host !== "agentcore" ? "health" : null;
  const { data: health } = useRuntimeRpc<HealthData>(healthMethod, undefined, {
    refreshInterval: (latest: HealthData | undefined) => latest?.ok === false ? HEALTH_POLL_DEGRADED : HEALTH_POLL_STEADY,
  });

  let state: HealthState;
  let reason: string;
  if (container?.lifecycle === "parked") {
    state = "PARKED";
    reason = "Agents asleep — they wake when you interact";
  } else if (!container) {
    state = "STARTING";
    reason = "Loading container status...";
  } else if (container.container_status === "provisioning") {
    state = "STARTING";
    reason = container.container_substatus === "auto_retry"
      ? "Restarting container..."
      : "Container provisioning — waiting for ECS task";
  } else if (container.container_status === "stopped" || container.container_status === "error") {
    state = "CONTAINER_DOWN";
    reason = container.last_error ?? `Container is ${container.container_status}`;
  } else if (health && !health.ok) {
    state = "GATEWAY_DOWN_DEAD";
    reason = "Runtime health check failing";
  } else if (!liveConnected) {
    state = "RECOVERING";
    reason = "Reconnecting live updates";
  } else {
    state = "HEALTHY";
    reason = "Connected";
  }

  const canRecover = state === "GATEWAY_DOWN_DEAD" || state === "CONTAINER_DOWN";
  const actionLabel = state === "CONTAINER_DOWN"
    ? "Restart Agent"
    : state === "GATEWAY_DOWN_DEAD"
      ? "Restart Gateway"
      : null;

  const recover = useCallback(async (): Promise<RecoverResponse | null> => {
    if (isRecoveringRef.current) return null;
    isRecoveringRef.current = true;
    setIsRecovering(true);
    try {
      return await api.post("/container/recover", {}) as RecoverResponse;
    } catch {
      return null;
    } finally {
      setTimeout(() => {
        isRecoveringRef.current = false;
        setIsRecovering(false);
      }, 5_000);
    }
  }, [api]);

  return { state, reason, canRecover, actionLabel, recover, isRecovering };
}
