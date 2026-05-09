"use client";

import { useEffect, useState, useCallback, useRef } from "react";
import { useGateway } from "@/hooks/useGateway";
import { useGatewayRpc } from "@/hooks/useGatewayRpc";
import { useProvisioningState } from "@/hooks/useProvisioningState";
import { useApi } from "@/lib/api";

export type HealthState =
  | "HEALTHY"
  | "STARTING"
  | "RECOVERING"
  | "GATEWAY_DOWN"
  | "CONTAINER_DOWN";

interface HealthData {
  ok?: boolean;
}

export interface SystemHealth {
  state: HealthState;
  reason: string;
  /** Whether a recovery action is available */
  canRecover: boolean;
  /** Label for the recovery button */
  actionLabel: string | null;
  /** Call to trigger recovery */
  recover: () => Promise<RecoverResponse | null>;
  /** Whether recovery is in progress */
  isRecovering: boolean;
}

interface RecoverResponse {
  action: "reprovision" | "gateway_restart" | "none" | "already_recovering";
  state: string;
  reason: string;
}

const MAX_RECONNECT = 10;

export function useSystemHealth(): SystemHealth {
  const { isConnected, reconnectAttempt, onEvent } = useGateway();
  const { container } = useProvisioningState({
    refreshInterval: 10_000,
    enabled: true,
  });

  // Gateway health RPC — only when WS connected
  const { data: health } = useGatewayRpc<HealthData>(
    isConnected ? "health" : null,
    undefined,
    { refreshInterval: 5_000 },
  );

  const [isRecovering, setIsRecovering] = useState(false);
  const isRecoveringRef = useRef(false);
  const [pushState, setPushState] = useState<{ state: string; reason: string } | null>(null);
  const pushTimerRef = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const api = useApi();

  // Listen for push status_change events via WS
  // Backend sends event name "status_change" with payload {state, reason}
  useEffect(() => {
    const unsub = onEvent((eventName: string, data: unknown) => {
      if (eventName === "status_change" && data) {
        const payload = data as { state?: string; reason?: string };
        setPushState({
          state: payload.state ?? "HEALTHY",
          reason: payload.reason ?? "",
        });
        // Clear push state after 15s (let polling take over)
        if (pushTimerRef.current) clearTimeout(pushTimerRef.current);
        pushTimerRef.current = setTimeout(() => setPushState(null), 15_000);
      }
    });
    return () => {
      unsub();
      if (pushTimerRef.current) clearTimeout(pushTimerRef.current);
    };
  }, [onEvent]);

  // Derive state (first match wins)
  let state: HealthState;
  let reason: string;

  // Push events take priority for immediate transitions
  if (pushState) {
    state = pushState.state as HealthState;
    reason = pushState.reason;
  } else if (!container) {
    state = "STARTING";
    reason = "Loading container status...";
  } else if (container.status === "provisioning") {
    state = "STARTING";
    reason = container.substatus === "auto_retry"
      ? "Restarting container..."
      : "Container provisioning — waiting for ECS task";
  } else if (container.status === "stopped" || container.status === "error") {
    state = "CONTAINER_DOWN";
    reason = container.last_error
      ?? `Container is ${container.status}`;
  } else if (!isConnected && reconnectAttempt > 0 && reconnectAttempt < MAX_RECONNECT) {
    state = "RECOVERING";
    reason = `Reconnecting... attempt ${reconnectAttempt} of ${MAX_RECONNECT}`;
  } else if (!isConnected && container.status === "running") {
    state = "GATEWAY_DOWN";
    reason = "Gateway not responding";
  } else if (isConnected && health && !health.ok) {
    state = "GATEWAY_DOWN";
    reason = "Gateway health check failing";
  } else {
    state = "HEALTHY";
    reason = "Connected";
  }

  // Recovery action
  const canRecover = state === "GATEWAY_DOWN" || state === "CONTAINER_DOWN";
  const actionLabel =
    state === "CONTAINER_DOWN"
      ? "Restart Agent"
      : state === "GATEWAY_DOWN"
        ? "Restart Gateway"
        : null;

  const recover = useCallback(async (): Promise<RecoverResponse | null> => {
    if (isRecoveringRef.current) return null;
    isRecoveringRef.current = true;
    setIsRecovering(true);
    try {
      const resp = await api.post("/container/recover", {});
      return resp as RecoverResponse;
    } catch {
      return null;
    } finally {
      // Keep recovering state for 5s to debounce spam clicks
      setTimeout(() => {
        isRecoveringRef.current = false;
        setIsRecovering(false);
      }, 5_000);
    }
  }, [api]);

  return {
    state,
    reason,
    canRecover,
    actionLabel,
    recover,
    isRecovering,
  };
}
