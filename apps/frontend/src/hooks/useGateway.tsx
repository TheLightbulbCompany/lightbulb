// frontend/src/hooks/useGateway.tsx
"use client";

import { createContext, useContext, useEffect, type ReactNode } from "react";
import { useAuth, useUser } from "@clerk/nextjs";
import {
  useGatewayConnection,
  type GatewayConnectionValue,
} from "@isol8/shared";
import { WS_URL } from "@/lib/api";

// The connection machine (reconnect/backoff, keepalive, RPC correlation,
// message routing) lives in @isol8/shared so web and mobile share one
// protocol implementation — see the mobile-app plan. This module is the web
// platform wrapper: React context + the Tauri desktop-webview effects.

// Re-exported so feature code keeps importing protocol types from here.
export type {
  ChatIncomingMessage,
  GatewayEvent,
  ToolResultBlock,
} from "@isol8/shared";

export type GatewayContextValue = GatewayConnectionValue;

const GatewayContext = createContext<GatewayContextValue | null>(null);

// =============================================================================
// Provider
// =============================================================================

export function GatewayProvider({ children }: { children: ReactNode }) {
  const { getToken } = useAuth();
  const { user } = useUser();

  // getToken/uuid identities are unstable across renders; the shared hook
  // routes them through refs internally, preserving the single-flight
  // connect contract (pinned by useGateway.connectRace.test.tsx).
  const connection = useGatewayConnection({
    getToken,
    wsUrl: WS_URL,
    uuid: () => crypto.randomUUID(),
  });
  const { isConnected, setNodeConnected } = connection;

  // ---- Tauri IPC: listen for node status from desktop app ----

  useEffect(() => {
    if (typeof window === "undefined") return;
    const tauri = // eslint-disable-next-line @typescript-eslint/no-explicit-any
        (window as any).__TAURI__;
    if (!tauri?.event?.listen) return;

    let unlisten: (() => void) | null = null;
    tauri.event.listen("node:status", (event: { payload: string }) => {
      setNodeConnected(event.payload === "connected");
    }).then((fn: () => void) => { unlisten = fn; });
    return () => { unlisten?.(); };
  }, [setNodeConnected]);

  // ---- Tauri desktop: push auth identity when Clerk user changes or WS reconnects ----
  //
  // Fetches a fresh token alongside the current user so identity and token
  // always travel together (important if user A signs out and user B signs
  // in — we can't have stale ref-captured displayName paired with a fresh
  // token).
  //
  // Also keyed on `isConnected` so every WebSocket open — initial OR
  // reconnect — re-pushes a fresh JWT to the desktop. Clerk tokens expire
  // on the order of a minute; without this, a dropped NodeClient in the
  // desktop app retries with stale auth after any network blip or server
  // restart that forces a reconnect. Decoupled from the connection machine
  // itself so the WS lifecycle isn't disturbed when Clerk resolves.
  useEffect(() => {
    if (typeof window === "undefined") return;
    const tauri = // eslint-disable-next-line @typescript-eslint/no-explicit-any
        (window as any).__TAURI__;
    if (!tauri?.core?.invoke) return;
    if (!user?.id) return;  // wait for Clerk to resolve before pushing
    if (!isConnected) return;  // only push once WS is open (ensures token fresh)

    let cancelled = false;
    getToken().then((token) => {
      if (cancelled || !token) return;
      tauri.core.invoke("send_auth_token", {
        token,
        displayName: user.fullName || user.firstName || "User",
        userId: user.id,
      }).catch(() => {});
    });
    return () => { cancelled = true; };
  }, [isConnected, user?.id, user?.fullName, user?.firstName, getToken]);

  return (
    <GatewayContext.Provider value={connection}>
      {children}
    </GatewayContext.Provider>
  );
}

// =============================================================================
// Hook
// =============================================================================

export function useGateway(): GatewayContextValue {
  const ctx = useContext(GatewayContext);
  if (!ctx) {
    throw new Error("useGateway must be used within a GatewayProvider");
  }
  return ctx;
}
