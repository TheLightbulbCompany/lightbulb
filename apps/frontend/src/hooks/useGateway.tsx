// frontend/src/hooks/useGateway.tsx
"use client";

import {
  createContext,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { useAuth, useUser } from "@clerk/nextjs";
import {
  useGatewayConnection,
  type GatewayConnectionValue,
} from "@isol8/shared";
import { WS_URL } from "@/lib/api";
import { readDesktopPermissionGrant } from "@/lib/desktopPermissions";
import { subscribeDesktopPermissions } from "@/lib/desktopPermissionsSubscribe";
import { mutate } from "swr";

import { lastInputAgeMs } from "@/lib/presence";
import { useContainerWake } from "@/hooks/useContainerWake";

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
  const [desktopPermissionRevision, setDesktopPermissionRevision] = useState(0);

  // getToken/uuid identities are unstable across renders; the shared hook
  // routes them through refs internally, preserving the single-flight
  // connect contract (pinned by useGateway.connectRace.test.tsx).
  const connection = useGatewayConnection({
    getToken,
    wsUrl: WS_URL,
    uuid: () => crypto.randomUUID(),
  });
  const { isConnected, setNodeConnected, isParked, park, unpark, onEvent, onChatMessage } = connection;
  const { requestWake } = useContainerWake();

  // ---- Idle park / presence wake ----
  //
  // The 30s keepalive ping otherwise holds the gateway WS open forever, so
  // the backend's disconnect-armed teardown never fires while a tab merely
  // EXISTS — the 2026-07-18 incident kept a trial container alive 24h with
  // 46 minutes of actual human presence. Park = deliberately release the
  // socket once nobody is plausibly here; the backend stops a trial
  // container ~3min later. Unpark + wake on the next real input. Paid
  // owners' containers never stop on disconnect, so for them this only
  // sheds an idle socket.
  //
  // "Nobody here": hidden tab for 2min, or visible but untouched for 10min —
  // AND no server traffic for 5min, so a long streaming run someone is
  // watching hands-off is never cut mid-flight (every delta bumps the
  // server-activity clock; the backend's own active-run vetoes are the
  // second line of defense).
  const lastServerEventAtRef = useRef(Date.now());
  useEffect(() => {
    const bump = () => {
      lastServerEventAtRef.current = Date.now();
    };
    const offEvent = onEvent(bump);
    const offChat = onChatMessage(bump);
    return () => {
      offEvent();
      offChat();
    };
  }, [onEvent, onChatMessage]);

  useEffect(() => {
    const HIDDEN_PARK_MS = 2 * 60_000;
    const VISIBLE_IDLE_PARK_MS = 10 * 60_000;
    const SERVER_QUIET_MS = 5 * 60_000;
    const id = setInterval(() => {
      if (isParked) return;
      const idleFor = lastInputAgeMs();
      const hidden = typeof document !== "undefined" && document.visibilityState === "hidden";
      const serverQuietFor = Date.now() - lastServerEventAtRef.current;
      const humanGone = hidden ? idleFor > HIDDEN_PARK_MS : idleFor > VISIBLE_IDLE_PARK_MS;
      if (humanGone && serverQuietFor > SERVER_QUIET_MS) {
        park();
      }
    }, 30_000);
    return () => clearInterval(id);
  }, [isParked, park]);

  useEffect(() => {
    if (!isParked) return;
    const onReturn = () => {
      unpark();
      void requestWake("interaction");
      // requestWake revalidates /container/status itself, but it can be a
      // cooldown no-op — revalidate here too so the stepper always re-syncs
      // on return (the reachedReady latch zeroes its own poll interval).
      void mutate("/container/status");
    };
    window.addEventListener("pointerdown", onReturn, { capture: true, passive: true });
    window.addEventListener("keydown", onReturn, { capture: true, passive: true });
    return () => {
      window.removeEventListener("pointerdown", onReturn, { capture: true });
      window.removeEventListener("keydown", onReturn, { capture: true });
    };
  }, [isParked, unpark, requestWake]);

  // Mount-with-presence: a human arriving at the app is intent — pre-warm a
  // parked container so it's usually ready before their first message. The
  // hook's own presence + cooldown gates make this a no-op for background
  // restores and remount churn.
  useEffect(() => {
    void requestWake("mount");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ---- Tauri IPC: listen for node status from desktop app ----

  useEffect(() => {
    if (typeof window === "undefined") return;
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const tauri = (window as any).__TAURI__;
    if (!tauri?.event?.listen) return;

    let unlisten: (() => void) | null = null;
    tauri.event
      .listen("node:status", (event: { payload: string }) => {
        setNodeConnected(event.payload === "connected");
      })
      .then((fn: () => void) => {
        unlisten = fn;
      });
    return () => {
      unlisten?.();
    };
  }, [setNodeConnected]);

  useEffect(() => {
    return subscribeDesktopPermissions(() =>
      setDesktopPermissionRevision((value) => value + 1),
    );
  }, []);

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
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const tauri = (window as any).__TAURI__;
    if (!tauri?.core?.invoke) return;
    if (!user?.id) return; // wait for Clerk to resolve before pushing
    if (!isConnected) return; // only push once WS is open (ensures token fresh)
    const desktopGrant = readDesktopPermissionGrant();
    if (!desktopGrant) return;

    let cancelled = false;
    getToken().then((token) => {
      if (cancelled || !token) return;
      tauri.core
        .invoke("send_auth_token", {
          token,
          displayName: user.fullName || user.firstName || "User",
          userId: user.id,
          permissions: desktopGrant.permissions,
        })
        .catch(() => {});
    });
    return () => {
      cancelled = true;
    };
  }, [
    isConnected,
    user?.id,
    user?.fullName,
    user?.firstName,
    getToken,
    desktopPermissionRevision,
  ]);

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
