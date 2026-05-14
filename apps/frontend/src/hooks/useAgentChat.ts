// frontend/src/hooks/useAgentChat.ts
/**
 * Agent chat hook that uses the shared GatewayProvider WebSocket.
 *
 * State model mirrors upstream OpenClaw control-ui's flat-message + parallel
 * arrays shape (chatMessages + chatStream + chatStreamSegments +
 * chatToolMessages + toolStreamById). Live thinking is preserved as an
 * Isol8-specific addition.
 *
 * Message protocol (unchanged):
 * - Send: { type: "agent_chat", agent_id: string, message: string }
 * - Receive: { type: "chunk", content: string, runId }
 * - Receive: { type: "thinking", content: string, runId }
 * - Receive: { type: "tool_start" | "tool_end" | "tool_error", ... }
 * - Receive: { type: "done", runId }
 * - Receive: { type: "error", message: string }
 * - Receive: { type: "heartbeat" }
 */

"use client";

import { useState, useCallback, useRef, useEffect, useMemo } from "react";
import {
  useGateway,
  type ChatIncomingMessage,
} from "@/hooks/useGateway";
import { capture } from "@/lib/analytics";
import type {
  HistoryMessage,
  ContentBlock,
  ToolStreamEntry,
  ChatRenderItem,
  RenderGroup,
} from "./internal/types";
import { projectChatHistoryMessages } from "./internal/chatHistoryFilters";
import { buildChatItems } from "./internal/buildChatItems";
import { groupBySide } from "./internal/groupBySide";

// Re-export types from internal modules for consumers.
export type {
  HistoryMessage,
  ContentBlock,
  ChatRenderItem,
  RenderGroup,
  RenderSide,
} from "./internal/types";

// =============================================================================
// Friendly error messages
//
// Patterns map low-level transport / startup errors to user-friendly copy.
// We deliberately do NOT mask backend-forwarded agent errors here — those
// carry rich detail from the LLM provider (rate limits, context-length,
// auth failures, etc.) that the user needs to see verbatim. The typed
// ErrorCard component in MessageList renders a tailored UI when the
// backend includes an `errorCode`; otherwise the raw upstream message
// is shown so the user has something actionable, not "encountered an
// error, please try again."
// =============================================================================

const ERROR_PATTERNS: [RegExp, string][] = [
  [/timed out during opening handshake/i, "Your agent is starting up — please try again in a moment."],
  [/Gateway not healthy/i, "Your agent is not ready yet. Please try again in a moment."],
  [/health check timed out/i, "Your agent is not responding. Please try again in a moment."],
  [/Gateway connection lost/i, "Lost connection to your agent. Retrying..."],
  [/Connection closed/i, "Connection to your agent was interrupted. Please try again."],
  [/session file locked/i, "Your agent is busy with another request. Please wait a moment and try again."],
  [/Model access is denied/i, "This model is not available. Try switching to a different model in settings."],
];

function friendlyError(raw: string): string {
  for (const [pattern, friendly] of ERROR_PATTERNS) {
    if (pattern.test(raw)) return friendly;
  }
  return raw;
}

// =============================================================================
// Types
// =============================================================================

export const BOOTSTRAP_MESSAGE =
  "Hello! Please run Bootstrap.md — I'd love to learn about everyday life use cases where you can be helpful and automate work for me.";

/**
 * A user-typed message that arrived while a previous turn was still in
 * flight. Held in `queuedMessages` and drained one-at-a-time on the
 * busy→idle transition. Mirrors upstream OpenClaw control-ui's chat
 * queue (~/Desktop/openclaw/ui/src/ui/app-chat.ts:181-217).
 */
export interface QueuedMessage {
  id: string;
  text: string;
  createdAt: number;
}

export interface UseAgentChatReturn {
  /** Final rendered groups, ready for MessageList to walk. */
  renderGroups: RenderGroup[];
  /** Raw flat items before grouping — exposed for snapshot testing and StreamingStatusBar. */
  renderItems: ChatRenderItem[];
  /** Whether a user-turn is in flight (chatStream non-null OR live tools running OR clientRunIdRef set). */
  isStreaming: boolean;
  error: string | null;
  sendMessage: (message: string) => Promise<void>;
  cancelMessage: () => Promise<void>;
  clearMessages: () => void;
  isConnected: boolean;
  isLoadingHistory: boolean;
  needsBootstrap: boolean;
  queuedMessages: QueuedMessage[];
  removeQueuedMessage: (id: string) => void;
  clearQueue: () => void;
}

// =============================================================================
// Module-level message cache
//
// Survives component unmounts (navigation away from /chat and back) but
// clears on full page refresh. Keyed by agentId so each agent's history
// is preserved independently.
// =============================================================================

const _messageCache = new Map<string, HistoryMessage[]>();
const _needsBootstrap = new Set<string>();

// =============================================================================
// Hook
//
// NOTE: Only one useAgentChat instance should be active at a time. The backend
// protocol does not tag chunk/done/error messages with an agent_id, so
// concurrent instances would receive each other's messages. The UI enforces
// this by rendering a single AgentChatWindow.
// =============================================================================

export function useAgentChat(agentId: string | null, sessionName: string): UseAgentChatReturn {
  const { isConnected, sendChat, onChatMessage, sendReq } = useGateway();

  // Cache key includes session name so org members don't share history cache
  const cacheKey = agentId ? `${agentId}:${sessionName}` : null;

  // Persisted messages — from chat.history + chat.final synthesis.
  const [chatMessages, setChatMessages] = useState<HistoryMessage[]>(
    () => (cacheKey ? _messageCache.get(cacheKey) ?? [] : []),
  );

  // Live state for the in-flight run. Refs mirror the state so the
  // onChatMessage handler (registered once per mount with stable deps)
  // can read the live values without stale-closure bugs.
  const [chatStream, _setChatStreamRaw] = useState<string | null>(null);
  const chatStreamRef = useRef<string | null>(null);
  const chatStreamRunIdRef = useRef<string | null>(null);
  const chatStreamStartedAtRef = useRef<number | null>(null);
  const setChatStream = useCallback((value: string | null) => {
    chatStreamRef.current = value;
    _setChatStreamRaw(value);
  }, []);

  // Live thinking — Isol8 addition.
  const [chatStreamThinking, _setChatStreamThinkingRaw] = useState<string | null>(null);
  const chatStreamThinkingRef = useRef<string | null>(null);
  const chatStreamThinkingRunIdRef = useRef<string | null>(null);
  const setChatStreamThinking = useCallback((value: string | null) => {
    chatStreamThinkingRef.current = value;
    _setChatStreamThinkingRaw(value);
  }, []);

  // Stream segments (text fragments committed when a tool interrupts streaming).
  const [chatStreamSegments, setChatStreamSegments] = useState<Array<{ text: string; ts: number }>>([]);

  // Live tool state.
  const [chatToolMessages, setChatToolMessages] = useState<HistoryMessage[]>([]);
  const toolStreamByIdRef = useRef<Map<string, ToolStreamEntry>>(new Map());
  const toolStreamOrderRef = useRef<string[]>([]);

  const [isStreaming, _setIsStreamingRaw] = useState(false);
  const isStreamingRef = useRef(false);
  const setIsStreaming = useCallback((value: boolean) => {
    isStreamingRef.current = value;
    _setIsStreamingRaw(value);
  }, []);

  const [error, setError] = useState<string | null>(null);
  const [historyLoadState, setHistoryLoadState] = useState<"idle" | "loading" | "done">("idle");
  const isLoadingHistory = historyLoadState === "loading";

  // Cancel-before-first-event window (preserved from previous design).
  const pendingCancelRef = useRef<boolean>(false);

  // Analytics counters.
  const turnStartedAtRef = useRef<number | null>(null);
  const turnAssistantLenRef = useRef<number>(0);

  // agentId mirror for callback access without stale closures.
  const agentIdRef = useRef(agentId);

  // Busy gate — preserved from PR #621. NO approval-pause guard from #628.
  const clientRunIdRef = useRef<string | null>(null);

  // Queue (preserved from #621).
  const [queuedMessages, setQueuedMessages] = useState<QueuedMessage[]>([]);
  const queuedMessagesRef = useRef<QueuedMessage[]>([]);
  useEffect(() => {
    queuedMessagesRef.current = queuedMessages;
  }, [queuedMessages]);

  const removeQueuedMessage = useCallback((id: string) => {
    setQueuedMessages((prev) => prev.filter((m) => m.id !== id));
  }, []);

  const clearQueue = useCallback(() => {
    setQueuedMessages([]);
  }, []);

  useEffect(() => {
    agentIdRef.current = agentId;
  }, [agentId]);

  // ---- Sync chatMessages to module-level cache ----
  useEffect(() => {
    if (cacheKey && chatMessages.length > 0) {
      _messageCache.set(cacheKey, chatMessages);
    }
  }, [cacheKey, chatMessages]);

  // ---- Tool stream sync (Map → state array) ----
  //
  // Sync toolStreamById Map → chatToolMessages array (state) for rendering.
  // Throttled at 80ms (matches upstream) but force-flushed on tool_end.
  const toolStreamSyncTimerRef = useRef<number | null>(null);
  const TOOL_STREAM_THROTTLE_MS = 80;

  const syncToolStreamMessages = useCallback(() => {
    const flush = () => {
      const ordered: HistoryMessage[] = toolStreamOrderRef.current
        .map((id) => toolStreamByIdRef.current.get(id))
        .filter((e): e is ToolStreamEntry => Boolean(e))
        .map((e) => ({
          role: "assistant",
          content: [
            {
              type: "tool_use",
              id: e.toolCallId,
              name: e.name,
              args: e.args,
            },
            ...(e.output !== undefined
              ? [
                  {
                    type: "tool_result",
                    tool_use_id: e.toolCallId,
                    content: e.output,
                    ...(e.status === "error" ? { isError: true } : {}),
                  } as ContentBlock,
                ]
              : []),
          ],
          timestamp: e.updatedAt,
          runId: e.runId,
        }));
      setChatToolMessages(ordered);
      toolStreamSyncTimerRef.current = null;
    };
    if (toolStreamSyncTimerRef.current !== null) {
      return; // already scheduled
    }
    // Force-flush immediately if any entry is in terminal state.
    const hasTerminal = Array.from(toolStreamByIdRef.current.values()).some(
      (e) => e.status !== "running",
    );
    if (hasTerminal) {
      flush();
      return;
    }
    toolStreamSyncTimerRef.current = window.setTimeout(flush, TOOL_STREAM_THROTTLE_MS);
  }, []);

  // Clean up the throttle timer on unmount.
  useEffect(() => {
    return () => {
      if (toolStreamSyncTimerRef.current !== null) {
        window.clearTimeout(toolStreamSyncTimerRef.current);
        toolStreamSyncTimerRef.current = null;
      }
    };
  }, []);

  // ---- Fetch history on mount / agent change ----
  const historyLoadedRef = useRef<Set<string>>(new Set());

  useEffect(() => {
    if (!agentId || !cacheKey || !isConnected) return;
    if (historyLoadedRef.current.has(cacheKey)) return;
    if (_messageCache.has(cacheKey)) {
      historyLoadedRef.current.add(cacheKey);
      return;
    }
    const sessionKey = `agent:${agentId}:${sessionName}`;
    Promise.resolve().then(() => setHistoryLoadState("loading"));
    sendReq("chat.history", { sessionKey, limit: 200 })
      .then((result: unknown) => {
        const historyResult = result as { messages?: HistoryMessage[] };
        historyLoadedRef.current.add(cacheKey);
        setHistoryLoadState("done");
        const raw = Array.isArray(historyResult?.messages) ? historyResult.messages : [];
        const projected = projectChatHistoryMessages(raw);
        if (projected.length === 0) {
          _needsBootstrap.add(cacheKey);
          return;
        }
        if (agentIdRef.current !== agentId) return;
        setChatMessages(projected);
        _messageCache.set(cacheKey, projected);
      })
      .catch((err: unknown) => {
        console.warn("Failed to fetch chat history:", err);
        historyLoadedRef.current.add(cacheKey);
        setHistoryLoadState("done");
      });
  }, [agentId, cacheKey, sessionName, isConnected, sendReq]);

  // ---- Chat message handler ----
  // Dependencies are intentionally minimal ([onChatMessage]) because all
  // mutable values are accessed through refs, avoiding stale closures.
  useEffect(() => {
    return onChatMessage((msg: ChatIncomingMessage) => {
      // Drop messages for a different agent (heartbeat / update_available
      // are broadcast; errors are allowed through for safety).
      const isUntaggedBroadcast = msg.type === "heartbeat" || msg.type === "update_available";
      const isUntaggedError = msg.type === "error" && msg.agent_id === undefined;
      if (!isUntaggedBroadcast && !isUntaggedError && msg.agent_id !== agentIdRef.current) {
        return;
      }

      if (msg.type === "chunk") {
        const runId = msg.runId;
        // Sub-run boundary: if the incoming chunk has a new runId, commit
        // the prior stream into chatStreamSegments first.
        if (chatStreamRunIdRef.current !== null && chatStreamRunIdRef.current !== runId) {
          const prior = chatStreamRef.current;
          if (prior && prior.length > 0) {
            setChatStreamSegments((segs) => [...segs, { text: prior, ts: Date.now() }]);
          }
        }
        setChatStream(msg.content);
        chatStreamRunIdRef.current = runId;
        if (chatStreamStartedAtRef.current === null) {
          chatStreamStartedAtRef.current = Date.now();
        }
        if (!isStreamingRef.current) setIsStreaming(true);
        return;
      }

      if (msg.type === "thinking") {
        const runId = msg.runId;
        if (chatStreamThinkingRunIdRef.current !== null && chatStreamThinkingRunIdRef.current !== runId) {
          // No need to commit prior thinking to segments — thinking is rendered
          // inside its bubble live; final assistant message will carry the
          // finalized thinking content if any.
        }
        setChatStreamThinking(msg.content);
        chatStreamThinkingRunIdRef.current = runId;
        if (!isStreamingRef.current) setIsStreaming(true);
        return;
      }

      if (msg.type === "tool_start") {
        const runId = msg.runId;
        const toolCallId = msg.toolCallId;
        if (!toolCallId) return;

        // Commit any in-progress chatStream to segments before the tool runs.
        const priorStream = chatStreamRef.current;
        if (priorStream && priorStream.length > 0) {
          setChatStreamSegments((segs) => [...segs, { text: priorStream, ts: Date.now() }]);
          setChatStream(null);
          chatStreamRunIdRef.current = null;
        }

        const now = Date.now();
        const entry: ToolStreamEntry = {
          toolCallId,
          runId,
          name: msg.tool,
          args: msg.args,
          status: "running",
          startedAt: now,
          updatedAt: now,
        };
        toolStreamByIdRef.current.set(toolCallId, entry);
        if (!toolStreamOrderRef.current.includes(toolCallId)) {
          toolStreamOrderRef.current.push(toolCallId);
        }
        syncToolStreamMessages();
        if (!isStreamingRef.current) setIsStreaming(true);
        return;
      }

      if (msg.type === "tool_end" || msg.type === "tool_error") {
        const toolCallId = msg.toolCallId;
        if (!toolCallId) return;
        const entry = toolStreamByIdRef.current.get(toolCallId);
        if (!entry) return;
        entry.output = msg.result;
        entry.status = msg.type === "tool_error" ? "error" : "done";
        entry.updatedAt = Date.now();
        toolStreamByIdRef.current.set(toolCallId, entry);
        syncToolStreamMessages();
        return;
      }

      if (msg.type === "done") {
        const runId = msg.runId;
        // Commit any pending stream into a synthesized assistant message.
        const stream = chatStreamRef.current;
        const thinking = chatStreamThinkingRef.current;
        if ((stream && stream.length > 0) || (thinking && thinking.length > 0)) {
          const content: ContentBlock[] = [];
          if (thinking) content.push({ type: "thinking", thinking });
          if (stream) content.push({ type: "text", text: stream });
          const synthesized: HistoryMessage = {
            role: "assistant",
            content,
            timestamp: Date.now(),
            runId,
          };
          setChatMessages((prev) => [...prev, synthesized]);
          turnAssistantLenRef.current += stream?.length ?? 0;
        }
        setChatStream(null);
        chatStreamRunIdRef.current = null;
        chatStreamStartedAtRef.current = null;
        setChatStreamThinking(null);
        chatStreamThinkingRunIdRef.current = null;

        // Busy-gate clear (no approval-pause guard).
        if (runId === clientRunIdRef.current) {
          clientRunIdRef.current = null;
        }

        // If the user-turn truly ended (no more live state), drop isStreaming.
        if (
          toolStreamByIdRef.current.size === 0 ||
          Array.from(toolStreamByIdRef.current.values()).every((e) => e.status !== "running")
        ) {
          if (clientRunIdRef.current === null) {
            setIsStreaming(false);
            const startedAt = turnStartedAtRef.current;
            if (startedAt !== null) {
              capture("chat_completed", {
                agent_id: agentIdRef.current ?? undefined,
                duration_ms: Date.now() - startedAt,
                assistant_message_length: turnAssistantLenRef.current,
              });
              turnStartedAtRef.current = null;
              turnAssistantLenRef.current = 0;
            }
          }
        }
        return;
      }

      if (msg.type === "error") {
        const displayError = friendlyError(msg.message);
        const errorCode = msg.code;
        // Append an assistant-role error message to chatMessages.
        const errMsg: HistoryMessage = {
          role: "assistant",
          content: [{ type: "text", text: `Error: ${displayError}` }],
          timestamp: Date.now(),
          runId: msg.runId,
          errorCode,
        };
        setChatMessages((prev) => [...prev, errMsg]);
        // Clear live state.
        setChatStream(null);
        chatStreamRunIdRef.current = null;
        setChatStreamThinking(null);
        chatStreamThinkingRunIdRef.current = null;
        setError(displayError);
        if (!msg.runId || msg.runId === clientRunIdRef.current) {
          clientRunIdRef.current = null;
        }
        // Drop isStreaming if no other activity.
        const stillRunning = Array.from(toolStreamByIdRef.current.values()).some(
          (e) => e.status === "running",
        );
        if (!stillRunning && clientRunIdRef.current === null) {
          setIsStreaming(false);
        }
        return;
      }

      // heartbeat / update_available — no chat-state mutation.
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [onChatMessage]);

  // ---- Send message ----

  // Hoisted so both sendMessage and the flushQueue effect can call it.
  // dispatchNow assumes the busy-gate has already been checked.
  const dispatchNow = useCallback(
    async (message: string): Promise<void> => {
      const agentId = agentIdRef.current;
      if (!agentId) return;
      if (!isConnected) {
        setError("Not connected. Please wait and try again.");
        return;
      }

      const newClientRunId = crypto.randomUUID();
      clientRunIdRef.current = newClientRunId;

      const userMsg: HistoryMessage = {
        role: "user",
        content: [{ type: "text", text: message }],
        timestamp: Date.now(),
      };
      setChatMessages((prev) => [...prev, userMsg]);
      setIsStreaming(true);

      turnStartedAtRef.current = Date.now();
      turnAssistantLenRef.current = 0;
      capture("chat_message_sent", {
        agent_id: agentId,
        message_length: message.length,
      });

      try {
        sendChat(agentId, message, newClientRunId);
      } catch (err) {
        const errorMessage = friendlyError(
          err instanceof Error ? err.message : "Failed to send message",
        );
        setError(errorMessage);
        const errMsg: HistoryMessage = {
          role: "assistant",
          content: [{ type: "text", text: `Error: ${errorMessage}` }],
          timestamp: Date.now(),
        };
        setChatMessages((prev) => [...prev, errMsg]);
        setIsStreaming(false);
        clientRunIdRef.current = null;
      }
    },
    [sendChat, isConnected, setIsStreaming],
  );

  // Drain queued messages one at a time on busy→idle transitions.
  // We track the previous value of clientRunIdRef in a ref so a simple
  // effect can detect the falling edge without polling.
  const prevClientRunIdRef = useRef<string | null>(null);
  // Deliberate no-deps effect: must run after every render so the ref-based
  // falling-edge detector catches the busy→idle transition. The work is
  // O(1) (two ref reads + comparison) when no transition occurred.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => {
    const prev = prevClientRunIdRef.current;
    const curr = clientRunIdRef.current;
    prevClientRunIdRef.current = curr;
    if (
      prev !== null &&
      curr === null &&
      queuedMessagesRef.current.length > 0 &&
      isConnected
    ) {
      const next = queuedMessagesRef.current[0];
      setQueuedMessages((q) => q.slice(1));
      void dispatchNow(next.text);
    }
  });

  const sendMessage = useCallback(
    async (message: string): Promise<void> => {
      pendingCancelRef.current = false;

      if (!agentIdRef.current) {
        throw new Error("No agent selected");
      }
      if (!isConnected) {
        setError("Not connected. Please wait and try again.");
        return;
      }

      const trimmed = message.trim();
      if (!trimmed) return;

      setError(null);
      const key = agentIdRef.current ? `${agentIdRef.current}:${sessionName}` : null;
      if (key) _needsBootstrap.delete(key);

      // Busy gate: if a user-turn is already in flight (clientRunIdRef
      // is set), enqueue instead of firing chat.send. The flushQueue
      // effect picks this up when clientRunIdRef clears.
      if (clientRunIdRef.current !== null) {
        const id = `queued-${crypto.randomUUID()}`;
        setQueuedMessages((prev) => [
          ...prev,
          { id, text: trimmed, createdAt: Date.now() },
        ]);
        return;
      }

      await dispatchNow(trimmed);
    },
    [isConnected, sessionName, dispatchNow],
  );

  // ---- Cancel / stop agent ----

  const cancelMessage = useCallback(async () => {
    if (!agentIdRef.current || !isStreaming) return;

    const startedAt = turnStartedAtRef.current;
    capture("chat_aborted", {
      agent_id: agentIdRef.current,
      elapsed_ms: startedAt !== null ? Date.now() - startedAt : 0,
    });
    turnStartedAtRef.current = null;
    turnAssistantLenRef.current = 0;

    const sessionKey = `agent:${agentIdRef.current}:${sessionName}`;
    try {
      await sendReq("chat.abort", { sessionKey });
    } catch (err) {
      console.warn("Failed to abort agent run:", err);
    }

    setIsStreaming(false);
    setChatStream(null);
    chatStreamRunIdRef.current = null;
    setChatStreamThinking(null);
    chatStreamThinkingRunIdRef.current = null;
    pendingCancelRef.current = true;
    clientRunIdRef.current = null;
    // Live tools stay rendered with their last-known status.
  }, [isStreaming, sendReq, sessionName, setIsStreaming]);

  // ---- Clear messages ----

  const clearMessages = useCallback(() => {
    setChatMessages([]);
    setChatStream(null);
    chatStreamRunIdRef.current = null;
    setChatStreamThinking(null);
    chatStreamThinkingRunIdRef.current = null;
    setChatStreamSegments([]);
    setChatToolMessages([]);
    toolStreamByIdRef.current.clear();
    toolStreamOrderRef.current = [];
    const key = agentIdRef.current ? `${agentIdRef.current}:${sessionName}` : null;
    if (key) _messageCache.delete(key);
    setError(null);
    setIsStreaming(false);
    pendingCancelRef.current = true;
    clientRunIdRef.current = null;
  }, [sessionName, setIsStreaming]);

  // ---- External interface ----

  // Compute render output from state. memo for stability.
  const renderItems = useMemo<ChatRenderItem[]>(
    () =>
      buildChatItems({
        chatMessages,
        chatStream,
        chatStreamRunId: chatStreamRunIdRef.current,
        chatStreamThinking,
        chatStreamThinkingRunId: chatStreamThinkingRunIdRef.current,
        chatStreamSegments,
        chatToolMessages,
        sessionKey: `agent:${agentId ?? ""}:${sessionName}`,
      }),
    [chatMessages, chatStream, chatStreamThinking, chatStreamSegments, chatToolMessages, agentId, sessionName],
  );

  const renderGroups = useMemo<RenderGroup[]>(() => groupBySide(renderItems), [renderItems]);

  const needsBootstrap = !!(
    cacheKey &&
    _needsBootstrap.has(cacheKey) &&
    chatMessages.length === 0 &&
    historyLoadState === "done"
  );

  return {
    renderGroups,
    renderItems,
    isStreaming,
    error,
    sendMessage,
    cancelMessage,
    clearMessages,
    isConnected,
    isLoadingHistory,
    needsBootstrap,
    queuedMessages,
    removeQueuedMessage,
    clearQueue,
  };
}
