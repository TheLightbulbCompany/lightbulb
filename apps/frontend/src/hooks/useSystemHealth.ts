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
  const { data: health } = useRuntimeRpc<HealthData>("health", undefined, {
    refreshInterval: (latest: HealthData | undefined) => latest?.ok === false ? HEALTH_POLL_DEGRADED : HEALTH_POLL_STEADY,
  });
  const { container } = useProvisioningState();

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
