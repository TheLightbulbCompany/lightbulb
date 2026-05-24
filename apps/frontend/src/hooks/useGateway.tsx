// frontend/src/hooks/useGateway.tsx
"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { useAuth, useUser } from "@clerk/nextjs";
import { WS_URL } from "@/lib/api";

// =============================================================================
// Constants
// =============================================================================

const MAX_RECONNECT_ATTEMPTS = 10;
const RECONNECT_DELAYS = [1000, 2000, 4000, 8000, 16000, 16000, 16000, 16000, 16000, 16000];
const PING_INTERVAL_MS = 30000;
const CONNECTION_TIMEOUT_MS = 10000;
const RPC_TIMEOUT_MS = 30000;

// =============================================================================
// Types
// =============================================================================

/**
 * OpenClaw tool result content blocks — text blocks or image refs. Image
 * data is replaced with `{ bytes, omitted: true }` by OpenClaw's sanitizer.
 */
export type ToolResultBlock = { type: string; text?: string; bytes?: number; omitted?: boolean };

export type ChatIncomingMessage =
  | { type: "chunk"; content: string; agent_id?: string; runId: string }
  | { type: "thinking"; content: string; agent_id?: string; runId: string }
  | { type: "done"; agent_id?: string; runId: string }
  | { type: "error"; message: string; code?: string; agent_id?: string; runId?: string }
  | { type: "heartbeat" }
  | {
      type: "tool_start";
      tool: string;
      toolCallId?: string;
      args?: Record<string, unknown>;
      agent_id?: string;
      runId: string;
    }
  | {
      type: "tool_end";
      tool: string;
      toolCallId?: string;
      result?: ToolResultBlock[];
      meta?: string;
      agent_id?: string;
      runId: string;
    }
  | {
      type: "tool_error";
      tool: string;
      toolCallId?: string;
      result?: ToolResultBlock[];
      meta?: string;
      agent_id?: string;
      runId: string;
    }
  | { type: "update_available" };

/** Gateway event forwarded from OpenClaw */
export interface GatewayEvent {
  type: "event";
  event: string;
  payload: unknown;
}

interface PendingRpc {
  resolve: (value: unknown) => void;
  reject: (reason: Error) => void;
  timeout: ReturnType<typeof setTimeout>;
}

interface GatewayContextValue {
  isConnected: boolean;
  nodeConnected: boolean;
  error: string | null;
  reconnectAttempt: number;
  send: (payload: unknown) => void;
  sendReq: (method: string, params?: Record<string, unknown>, timeoutMs?: number) => Promise<unknown>;
  sendChat: (agentId: string, message: string, clientRunId: string) => void;
  onEvent: (handler: (event: string, data: unknown) => void) => () => void;
  onChatMessage: (handler: (msg: ChatIncomingMessage) => void) => () => void;
  reconnect: () => void;
}

const GatewayContext = createContext<GatewayContextValue | null>(null);

// =============================================================================
// Provider
// =============================================================================

export function GatewayProvider({ children }: { children: ReactNode }) {
  const { getToken } = useAuth();
  const { user } = useUser();
  const [isConnected, setIsConnected] = useState(false);
  const [nodeConnected, setNodeConnected] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [reconnectAttempt, setReconnectAttempt] = useState(0);

  const wsRef = useRef<WebSocket | null>(null);
  const reconnectAttemptRef = useRef(0);
  const reconnectTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pingIntervalRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const pendingRpcsRef = useRef<Map<string, PendingRpc>>(new Map());
  const eventHandlersRef = useRef<Set<(event: string, data: unknown) => void>>(new Set());
  const chatHandlersRef = useRef<Set<(msg: ChatIncomingMessage) => void>>(new Set());
  const connectRef = useRef<(() => Promise<void>) | undefined>(undefined);

  // ---- Cleanup helpers ----

  const clearPingInterval = useCallback(() => {
    if (pingIntervalRef.current) {
      clearInterval(pingIntervalRef.current);
      pingIntervalRef.current = null;
    }
  }, []);

  const clearReconnectTimeout = useCallback(() => {
    if (reconnectTimeoutRef.current) {
      clearTimeout(reconnectTimeoutRef.current);
      reconnectTimeoutRef.current = null;
    }
  }, []);

  // ---- Message router ----

  const handleMessage = useCallback((event: MessageEvent) => {
    if (!event.data || typeof event.data !== "string") return;
    let data: Record<string, unknown>;
    try {
      data = JSON.parse(event.data);
    } catch {
      return;
    }

    const msgType = data.type as string;

    // OpenClaw res — resolve pending RPC
    if (msgType === "res" && typeof data.id === "string") {
      const pending = pendingRpcsRef.current.get(data.id);
      if (pending) {
        clearTimeout(pending.timeout);
        pendingRpcsRef.current.delete(data.id);
        if (data.ok) {
          pending.resolve(data.payload);
        } else {
          const errObj = data.error as Record<string, unknown> | undefined;
          const errMsg = errObj?.message || "RPC call failed";
          // Surface additional detail (issues, details, code) if present
          const parts: string[] = [String(errMsg)];
          if (errObj?.code) parts.push(`[${errObj.code}]`);
          if (errObj?.details !== undefined && errObj.details !== null) {
            const d = errObj.details;
            if (typeof d === "string") {
              parts.push(d);
            } else {
              try { parts.push(JSON.stringify(d)); } catch { parts.push(String(d)); }
            }
          }
          if (errObj && Array.isArray(errObj.issues)) {
            const issueTexts = (errObj.issues as { path?: string; message?: string }[])
              .map(i => i.path ? `${i.path}: ${i.message}` : i.message)
              .filter(Boolean);
            if (issueTexts.length) parts.push(issueTexts.join("; "));
          }
          pending.reject(new Error(parts.join(" — ")));
        }
      }
      return;
    }

    // OpenClaw event — dispatch to subscribers
    if (msgType === "event") {
      const eventName = data.event as string;
      for (const handler of eventHandlersRef.current) {
        try {
          handler(eventName, data.payload);
        } catch {
          // subscriber error, ignore
        }
      }
      return;
    }

    // Node status — update desktop node connection state
    if (msgType === "node_status") {
      setNodeConnected(data.status === "connected");
      return;
    }

    // Chat messages — dispatch to subscribers
    if (
      msgType === "chunk" ||
      msgType === "thinking" ||
      msgType === "done" ||
      msgType === "error" ||
      msgType === "heartbeat" ||
      msgType === "tool_start" ||
      msgType === "tool_end" ||
      msgType === "tool_error" ||
      msgType === "update_available"
    ) {
      const chatMsg = data as unknown as ChatIncomingMessage;
      for (const handler of chatHandlersRef.current) {
        try {
          handler(chatMsg);
        } catch {
          // subscriber error, ignore
        }
      }
      return;
    }

    // pong — nothing to do
  }, []);

  // ---- Connect ----

  const connect = useCallback(async () => {
    if (
      wsRef.current?.readyState === WebSocket.OPEN ||
      wsRef.current?.readyState === WebSocket.CONNECTING
    ) {
      return;
    }

    try {
      const token = await getToken();
      if (!token) throw new Error("Not authenticated");

      // NOTE: the Tauri `send_auth_token` IPC used to fire from here too,
      // but that tied `connect` to `user.*` and tore the WebSocket down
      // every time Clerk resolved the user on mount — which silently
      // killed in-flight RPCs (notably agents.list on a returning user).
      // The desktop-identity push now runs in its own effect below,
      // keyed on user.id — so identity and token are always fetched
      // together and the WS lifecycle is decoupled from user changes.

      const ws = new WebSocket(`${WS_URL}?token=${token}`);

      ws.onopen = () => {
        reconnectAttemptRef.current = 0;
        setReconnectAttempt(0);
        setIsConnected(true);
        setError(null);

        clearPingInterval();
        pingIntervalRef.current = setInterval(() => {
          if (ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ type: "ping" }));
          }
        }, PING_INTERVAL_MS);
      };

      ws.onclose = (event) => {
        wsRef.current = null;
        setIsConnected(false);
        clearPingInterval();

        // Reject all pending RPCs
        for (const [, pending] of pendingRpcsRef.current) {
          clearTimeout(pending.timeout);
          pending.reject(new Error("WebSocket closed"));
        }
        pendingRpcsRef.current.clear();

        if (event.code === 1000 || event.code === 4001) {
          if (event.code === 4001) {
            setError("Authentication failed. Please refresh the page.");
          }
          return;
        }

        if (reconnectAttemptRef.current < MAX_RECONNECT_ATTEMPTS) {
          const delay =
            RECONNECT_DELAYS[reconnectAttemptRef.current] || 16000;
          reconnectAttemptRef.current++;
          setReconnectAttempt(reconnectAttemptRef.current);
          reconnectTimeoutRef.current = setTimeout(() => connectRef.current?.(), delay);
        } else {
          setError("Connection lost. Waiting for container...");
        }
      };

      ws.onerror = () => {};
      ws.onmessage = handleMessage;
      wsRef.current = ws;
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to connect");
    }
  }, [getToken, handleMessage, clearPingInterval]);

  // Keep ref in sync for stable reconnect closure
  connectRef.current = connect;

  const reconnect = useCallback(() => {
    if (wsRef.current) {
      wsRef.current.close(1000, "Manual reconnect");
      wsRef.current = null;
    }
    reconnectAttemptRef.current = 0;
    setReconnectAttempt(0);
    setError(null);
    clearReconnectTimeout();
    connect();
  }, [connect, clearReconnectTimeout]);

  // ---- Auto-connect on mount ----

  useEffect(() => {
    connect();
    return () => {
      clearReconnectTimeout();
      clearPingInterval();
      if (wsRef.current) {
        wsRef.current.close(1000, "Provider unmounted");
        wsRef.current = null;
      }
    };
  }, [connect, clearReconnectTimeout, clearPingInterval]);

  // ---- Electron IPC: listen for node status from desktop app ----

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
  // restart that forces a reconnect. Decoupled from `connect` itself so
  // the WS lifecycle isn't disturbed when Clerk resolves.
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

  // ---- sendReq ----

  const sendReq = useCallback(
    async (method: string, params?: Record<string, unknown>, timeoutMs?: number): Promise<unknown> => {
      // Ensure connected — use event listener instead of polling
      if (wsRef.current?.readyState !== WebSocket.OPEN) {
        await connect();
        await new Promise<void>((resolve, reject) => {
          const ws = wsRef.current;
          if (!ws) return reject(new Error("No WebSocket"));
          if (ws.readyState === WebSocket.OPEN) return resolve();

          const timeout = setTimeout(() => {
            ws.removeEventListener("open", onOpen);
            reject(new Error("Connection timeout"));
          }, CONNECTION_TIMEOUT_MS);

          function onOpen() {
            clearTimeout(timeout);
            resolve();
          }
          ws.addEventListener("open", onOpen, { once: true });
        });
      }

      const ws = wsRef.current;
      if (!ws || ws.readyState !== WebSocket.OPEN) {
        throw new Error("WebSocket closed before send");
      }

      const id = crypto.randomUUID();

      return new Promise((resolve, reject) => {
        const timeout = setTimeout(() => {
          pendingRpcsRef.current.delete(id);
          reject(new Error(`RPC timeout: ${method}`));
        }, timeoutMs ?? RPC_TIMEOUT_MS);

        pendingRpcsRef.current.set(id, { resolve, reject, timeout });

        ws.send(
          JSON.stringify({ type: "req", id, method, params: params || {} }),
        );
      });
    },
    [connect],
  );

  // ---- sendChat ----

  const sendChat = useCallback(
    (agentId: string, message: string, clientRunId: string) => {
      // Throw on closed socket instead of silently no-op'ing. Callers like
      // useAgentChat.dispatchNow rely on a thrown error to reset their per-
      // user-turn busy gate (clientRunIdRef). Silent no-op leaves the turn
      // marked in-flight with no chat.final ever arriving, wedging the
      // local chat queue.
      if (wsRef.current?.readyState !== WebSocket.OPEN) {
        throw new Error("Gateway connection lost");
      }
      wsRef.current.send(
        JSON.stringify({
          type: "agent_chat",
          agent_id: agentId,
          message,
          clientRunId,
        }),
      );
    },
    [],
  );

  // ---- send (raw fire-and-forget) ----

  // Fire-and-forget send for best-effort signals — currently the
  // teams.subscribe / teams.unsubscribe envelopes from
  // TeamsEventsProvider, but kept generic so future ws-broker
  // subscription kinds can reuse it. Silent no-op when the socket
  // isn't open; callers retry on their own cadence (TeamsEventsProvider
  // re-fires on isConnected transitions).
  const send = useCallback((payload: unknown): void => {
    const ws = wsRef.current;
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      return;
    }
    ws.send(JSON.stringify(payload));
  }, []);

  // ---- Subscription helpers ----

  const onEvent = useCallback(
    (handler: (event: string, data: unknown) => void) => {
      eventHandlersRef.current.add(handler);
      return () => {
        eventHandlersRef.current.delete(handler);
      };
    },
    [],
  );

  const onChatMessage = useCallback(
    (handler: (msg: ChatIncomingMessage) => void) => {
      chatHandlersRef.current.add(handler);
      return () => {
        chatHandlersRef.current.delete(handler);
      };
    },
    [],
  );

  const value = useMemo(
    () => ({ isConnected, nodeConnected, error, reconnectAttempt, send, sendReq, sendChat, onEvent, onChatMessage, reconnect }),
    [isConnected, nodeConnected, error, reconnectAttempt, send, sendReq, sendChat, onEvent, onChatMessage, reconnect],
  );

  return (
    <GatewayContext.Provider value={value}>
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
