import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { ChatIncomingMessage } from "./types";

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
 * Platform inputs for the gateway connection machine. All three are routed
 * through refs internally, so unstable identities (Clerk's `getToken` churns
 * as auth state resolves on mount) never re-run the connect effect — the
 * single-flight contract pinned by the web's useGateway.connectRace tests.
 */
export interface GatewayConnectionOptions {
  /** Fresh Clerk bearer token, or null when unauthenticated. */
  getToken: () => Promise<string | null>;
  /** WebSocket gateway origin (wss://ws-<env>.isol8.co). */
  wsUrl: string;
  /** Lowercase RFC-4122 v4 UUID source — used for RPC correlation ids. */
  uuid: () => string;
}

export interface GatewayConnectionValue {
  isConnected: boolean;
  nodeConnected: boolean;
  error: string | null;
  reconnectAttempt: number;
  /** Fire-and-forget send; returns whether the payload was actually written
   * (false when the socket isn't open — lets delivery-sensitive callers like
   * cancelMessage detect the stale-isConnected race and park a retry). */
  send: (payload: unknown) => boolean;
  sendReq: (method: string, params?: Record<string, unknown>, timeoutMs?: number) => Promise<unknown>;
  sendChat: (
    agentId: string,
    message: string,
    clientRunId: string,
    sessionId?: string,
  ) => void;
  onEvent: (handler: (event: string, data: unknown) => void) => () => void;
  onChatMessage: (handler: (msg: ChatIncomingMessage) => void) => () => void;
  reconnect: () => void;
  /**
   * Platform escape hatch: lets a wrapper layer feed node-connection state
   * from outside the socket (the desktop webview learns it via Tauri IPC).
   * The machine itself also sets it from `node_status` frames.
   */
  setNodeConnected: (connected: boolean) => void;
}

interface PendingRpc {
  resolve: (value: unknown) => void;
  reject: (reason: Error) => void;
  timeout: ReturnType<typeof setTimeout>;
}

// =============================================================================
// Hook
// =============================================================================

/**
 * The gateway WebSocket connection machine, extracted verbatim from the web
 * frontend's GatewayProvider (Phase 1 of the mobile-app plan): reconnect with
 * capped backoff, keepalive pings, pending-RPC correlation, and the message
 * router dispatching chat frames / OpenClaw events to subscribers.
 *
 * Platform wrappers own everything host-specific: React context, Tauri IPC,
 * and (on mobile) AppState-driven foreground reconnects layered on top via
 * `reconnect`.
 */
export function useGatewayConnection(options: GatewayConnectionOptions): GatewayConnectionValue {
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
  // Clerk's `getToken` changes identity as auth state resolves on mount.
  // Routing it through a ref keeps `connect` referentially stable so the
  // auto-connect effect runs exactly once — otherwise the effect re-fires
  // mid-handshake and the page ends up with two live sockets (observed as
  // same-second `WS connect` pairs in the backend logs on every page load,
  // with every broadcast event delivered twice through the orphan).
  const getTokenRef = useRef(options.getToken);
  getTokenRef.current = options.getToken;
  // wsUrl and uuid get the same ref treatment purely for effect stability —
  // they're constants in practice, but an inline `uuid: () => ...` lambda
  // must not invalidate sendReq's identity every render.
  const wsUrlRef = useRef(options.wsUrl);
  wsUrlRef.current = options.wsUrl;
  const uuidRef = useRef(options.uuid);
  uuidRef.current = options.uuid;
  // Single-flight gate for connect(): the token fetch suspends BEFORE
  // wsRef is assigned, so the readyState guard alone cannot see an
  // in-flight attempt. Concurrent callers (mount effect, sendReq,
  // reconnect) share one handshake.
  const connectInFlightRef = useRef<Promise<void> | null>(null);

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

  // RPCs in flight on a socket that is going away can never complete —
  // reject them promptly rather than letting them sit until the 30s RPC
  // timeout. Called from the live socket's onclose AND from every path
  // that retires a socket locally (manual reconnect, supersede): those
  // paths assign a new socket before the old one's async close event
  // fires, so the onclose stale-socket guard would skip this cleanup.
  const rejectPendingRpcs = useCallback((reason: string) => {
    for (const [, pending] of pendingRpcsRef.current) {
      clearTimeout(pending.timeout);
      pending.reject(new Error(reason));
    }
    pendingRpcsRef.current.clear();
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
      msgType === "run_ended" ||
      msgType === "heartbeat" ||
      msgType === "tool_start" ||
      msgType === "tool_end" ||
      msgType === "tool_error" ||
      msgType === "tool_progress" ||
      msgType === "command_output" ||
      msgType === "agent_activity" ||
      msgType === "btw_result" ||
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
    if (connectInFlightRef.current) {
      return connectInFlightRef.current;
    }

    const attempt = (async () => {
      try {
        const token = await getTokenRef.current();
        if (!token) throw new Error("Not authenticated");

        const ws = new WebSocket(`${wsUrlRef.current}?token=${token}`);

        ws.onopen = () => {
          if (wsRef.current !== ws) return; // superseded while opening
          console.info("[gateway] ws open");
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
          // A socket that is no longer the live one (superseded by a newer
          // connection) must not tear down state or schedule a reconnect
          // over it. wsRef === null means an explicit local teardown
          // (unmount / manual reconnect) — that close still runs the
          // cleanup body so pending RPCs reject promptly.
          if (wsRef.current !== null && wsRef.current !== ws) return;
          console.info(`[gateway] ws close code=${event.code} reason=${event.reason || "-"}`);
          wsRef.current = null;
          setIsConnected(false);
          clearPingInterval();

          rejectPendingRpcs("WebSocket closed");

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
            console.info(`[gateway] reconnect #${reconnectAttemptRef.current} in ${delay}ms`);
            reconnectTimeoutRef.current = setTimeout(() => connectRef.current?.(), delay);
          } else {
            setError("Connection lost. Waiting for container...");
          }
        };

        ws.onerror = () => {};
        ws.onmessage = handleMessage;
        if (wsRef.current && wsRef.current !== ws) {
          // Should be unreachable under the single-flight gate, but never
          // orphan a previous socket — an orphan keeps its handlers and
          // double-delivers every broadcast event.
          wsRef.current.close(1000, "Superseded by newer connection");
          rejectPendingRpcs("WebSocket closed");
        }
        wsRef.current = ws;
      } catch (err) {
        console.info(`[gateway] connect failed: ${err instanceof Error ? err.message : err}`);
        setError(err instanceof Error ? err.message : "Failed to connect");
      }
    })();

    connectInFlightRef.current = attempt;
    try {
      await attempt;
    } finally {
      connectInFlightRef.current = null;
    }
  }, [handleMessage, clearPingInterval, rejectPendingRpcs]);

  // Keep ref in sync for stable reconnect closure
  connectRef.current = connect;

  const reconnect = useCallback(() => {
    if (wsRef.current) {
      wsRef.current.close(1000, "Manual reconnect");
      wsRef.current = null;
      rejectPendingRpcs("WebSocket closed");
    }
    reconnectAttemptRef.current = 0;
    setReconnectAttempt(0);
    setError(null);
    clearReconnectTimeout();
    connect();
  }, [connect, clearReconnectTimeout, rejectPendingRpcs]);

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

      const id = uuidRef.current();

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
    (agentId: string, message: string, clientRunId: string, sessionId?: string) => {
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
          // Omitted for the General session so the backend builds the legacy
          // 3-part key; present only for a real session (validated server-side
          // against _SESSION_ID_RE before the 4-part key is constructed).
          ...(sessionId ? { session_id: sessionId } : {}),
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
  const send = useCallback((payload: unknown): boolean => {
    const ws = wsRef.current;
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      return false;
    }
    ws.send(JSON.stringify(payload));
    return true;
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

  return useMemo(
    () => ({
      isConnected,
      nodeConnected,
      error,
      reconnectAttempt,
      send,
      sendReq,
      sendChat,
      onEvent,
      onChatMessage,
      reconnect,
      setNodeConnected,
    }),
    [isConnected, nodeConnected, error, reconnectAttempt, send, sendReq, sendChat, onEvent, onChatMessage, reconnect],
  );
}
