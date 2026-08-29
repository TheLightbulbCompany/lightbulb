import { useAuth } from "@clerk/clerk-expo";
import { createContext, useContext, useEffect, useRef, type ReactNode } from "react";
import { AppState } from "react-native";
import { randomUUID } from "expo-crypto";
import {
  useGatewayConnection,
  type GatewayConnectionValue,
} from "@lightbulb/shared";

import { WS_URL } from "../config/env";
import { useContainerWake } from "../lib/containerWake";

// Mobile platform wrapper around the shared gateway connection machine.
// The web equivalent lives in apps/frontend/src/hooks/useGateway.tsx; this
// one maps wake-on-intent onto the iOS lifecycle. AppState is a cleaner
// presence signal than the web's visibility+input heuristic: iOS only makes
// an app "active" on a deliberate switch, so foreground IS intent and an idle
// foreground app auto-locks (→ background) within a minute or two.
//
//   • background/inactive → park(): release the socket. The backend's
//     disconnect-armed teardown then stops a trial container ~3min later —
//     so a backgrounded app stops holding its container up (and its
//     gateway RPCs freeze at their last cached value, via the shared
//     useGatewayRpc's parked-key null).
//   • active → unpark() + requestWake("mount"): the user came back; reconnect
//     and wake a parked container so it's ready by the time they act.
const GatewayContext = createContext<GatewayConnectionValue | null>(null);

export function GatewayProvider({ children }: { children: ReactNode }) {
  const { getToken } = useAuth();
  const { requestWake } = useContainerWake();

  const connection = useGatewayConnection({
    getToken,
    wsUrl: WS_URL,
    uuid: () => randomUUID(),
  });

  // Refs so the AppState listener is registered exactly once yet always calls
  // the latest closures.
  const parkRef = useRef(connection.park);
  parkRef.current = connection.park;
  const unparkRef = useRef(connection.unpark);
  unparkRef.current = connection.unpark;
  const isParkedRef = useRef(connection.isParked);
  isParkedRef.current = connection.isParked;
  const isConnectedRef = useRef(connection.isConnected);
  isConnectedRef.current = connection.isConnected;
  const reconnectRef = useRef(connection.reconnect);
  reconnectRef.current = connection.reconnect;
  const requestWakeRef = useRef(requestWake);
  requestWakeRef.current = requestWake;

  // First app open: AppState is already "active", so no "change" event fires
  // for it — pre-warm the container once on mount (parity with the web
  // provider's mount wake). The hook's presence gate + cooldown make this a
  // no-op when not foregrounded or already just-woken.
  useEffect(() => {
    void requestWakeRef.current("mount");
  }, []);

  useEffect(() => {
    const sub = AppState.addEventListener("change", (state) => {
      if (state === "active") {
        // Foreground = intent. unpark() reconnects (it also resets backoff and
        // tears down whatever half-dead socket iOS left behind); the wake
        // starts a parked container in parallel. If we're NOT parked but the
        // socket is dead anyway — a foreground network blip / server restart
        // whose reconnect backoff has since exhausted — kick a reconnect. This
        // is the pre-existing foreground-recovery fallback; the park refactor
        // must not drop it.
        if (isParkedRef.current) {
          unparkRef.current();
        } else if (!isConnectedRef.current) {
          reconnectRef.current();
        }
        void requestWakeRef.current("mount");
      } else if (state === "background") {
        // Real backgrounding (home / app-switch away) — release the socket so
        // the backend stops the trial container. Deliberately NOT "inactive":
        // that is a transient overlay (control center, notification shade,
        // incoming call) that usually returns straight to active, and parking
        // on it would churn a reconnect for every peek. If an inactive state
        // does proceed to background, this fires then.
        parkRef.current();
      }
    });
    return () => sub.remove();
  }, []);

  return (
    <GatewayContext.Provider value={connection}>
      {children}
    </GatewayContext.Provider>
  );
}

export function useGateway(): GatewayConnectionValue {
  const ctx = useContext(GatewayContext);
  if (!ctx) {
    throw new Error("useGateway must be used within a GatewayProvider");
  }
  return ctx;
}
