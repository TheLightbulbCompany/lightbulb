"use client";

import { useCallback, useRef, useState } from "react";
import { useLiveEvents } from "@/hooks/useLiveEvents";
import { useProvisioningState } from "@/hooks/useProvisioningState";
import { useApi } from "@/lib/api";
import { notifyApiError } from "@/lib/notify";

export type HealthState =
  | "HEALTHY"
  | "PARKED"
  | "STARTING"
  | "RECOVERING"
  | "GATEWAY_BUSY"
  | "CONTAINER_DOWN";

interface RecoverResponse {
  action: "reprovision" | "none" | "already_recovering";
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

export function useSystemHealth(): SystemHealth {
  const { liveConnected } = useLiveEvents();
  const [isRecovering, setIsRecovering] = useState(false);
  const isRecoveringRef = useRef(false);
  const api = useApi();
  const { container } = useProvisioningState();
  // No `health` probe: AgentCore parks an idle microVM invisibly and wakes it on
  // ANY invoke, so a poll would hold a box awake (and billed) for a dot whose
  // answer `runtime_phase` already gives unconditionally. The backend composer
  // refuses the same call for the same reason -- see "never probe a running
  // AgentCore row" in readiness_state.compute_status.

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
      ? "Restarting agents..."
      : "Provisioning your agents…";
  } else if (container.container_status === "stopped" || container.container_status === "error") {
    state = "CONTAINER_DOWN";
    reason = container.last_error ?? `Container is ${container.container_status}`;
  } else if (!liveConnected) {
    state = "RECOVERING";
    reason = "Reconnecting live updates";
  } else {
    state = "HEALTHY";
    reason = "Connected";
  }

  const canRecover = state === "CONTAINER_DOWN";
  const actionLabel = canRecover ? "Restart Agent" : null;

  const recover = useCallback(async (): Promise<RecoverResponse | null> => {
    if (isRecoveringRef.current) return null;
    isRecoveringRef.current = true;
    setIsRecovering(true);
    try {
      return await api.post("/container/recover", {}) as RecoverResponse;
    } catch (err) {
      notifyApiError(err, "Couldn't restart your agents");
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
