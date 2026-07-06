import { useAuth } from "@clerk/clerk-expo";
import { createContext, useContext, useEffect, useRef, type ReactNode } from "react";
import { AppState } from "react-native";
import { randomUUID } from "expo-crypto";
import {
  useGatewayConnection,
  type GatewayConnectionValue,
} from "@isol8/shared";

import { WS_URL } from "../config/env";

// Mobile platform wrapper around the shared gateway connection machine.
// The web equivalent lives in apps/frontend/src/hooks/useGateway.tsx; this
// one swaps Tauri effects for the iOS lifecycle concern: the OS suspends
// sockets when the app backgrounds, so we force a reconnect check on every
// return to foreground (plan R2).
const GatewayContext = createContext<GatewayConnectionValue | null>(null);

export function GatewayProvider({ children }: { children: ReactNode }) {
  const { getToken } = useAuth();

  const connection = useGatewayConnection({
    getToken,
    wsUrl: WS_URL,
    uuid: () => randomUUID(),
  });

  // `reconnect` resets the backoff counter and tears down whatever half-dead
  // socket iOS left behind. Routed through a ref so the AppState listener is
  // registered exactly once.
  const reconnectRef = useRef(connection.reconnect);
  reconnectRef.current = connection.reconnect;
  const isConnectedRef = useRef(connection.isConnected);
  isConnectedRef.current = connection.isConnected;

  useEffect(() => {
    const sub = AppState.addEventListener("change", (state) => {
      if (state === "active" && !isConnectedRef.current) {
        reconnectRef.current();
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
