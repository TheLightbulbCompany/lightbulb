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
// foldLiveTurnIntoContent
//
// Pure helper: takes a snapshot of the live-turn state (segments + tools +
// tail stream/thinking) and produces an ordered ContentBlock[] suitable for
// folding into a single assistant HistoryMessage. Used by the `done` handler
// AND by cancelMessage so that orphan segments/tools never linger past the
// end of a turn — leaving them in the live arrays would cause them to render
// AFTER the next user message at the buildChatItems tail (cross-turn
// ordering bug).
// =============================================================================

function foldLiveTurnIntoContent(params: {
  segments: Array<{ text: string; ts: number }>;
  toolEntries: Map<string, ToolStreamEntry>;
  toolOrder: string[];
  tailStream: string | null;
  tailThinking: string | null;
}): ContentBlock[] {
  const { segments, toolEntries, toolOrder, tailStream, tailThinking } = params;
  const toolBlocks: ContentBlock[][] = toolOrder
    .map((id) => toolEntries.get(id))
    .filter((e): e is ToolStreamEntry => Boolean(e))
    .map((e) => {
      const blocks: ContentBlock[] = [
        { type: "tool_use", id: e.toolCallId, name: e.name, args: e.args },
      ];
      // Emit tool_result whenever the tool has reached a terminal state,
      // even if the backend produced no payload — MessageBlocks derives
      // tool status from tool_use ↔ tool_result pairing in the flat
      // model, so a "done" tool with no output but no tool_result block
      // would otherwise render as still-running forever.
      if (e.status !== "running") {
        blocks.push({
          type: "tool_result",
          tool_use_id: e.toolCallId,
          content: e.output ?? "",
          ...(e.status === "error" ? { isError: true } : {}),
        });
      }
      return blocks;
    });

  const content: ContentBlock[] = [];
  const n = Math.max(segments.length, toolBlocks.length);
  for (let i = 0; i < n; i++) {
    const seg = segments[i];
    if (seg && seg.text.length > 0) content.push({ type: "text", text: seg.text });
    const tool = toolBlocks[i];
    if (tool) content.push(...tool);
  }
  if (tailThinking && tailThinking.length > 0) {
    content.push({ type: "thinking", thinking: tailThinking });
  }
  if (tailStream && tailStream.length > 0) {
    content.push({ type: "text", text: tailStream });
  }
  return content;
}

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
  // Cumulative-replacement guard: OpenClaw chunks deliver the FULL assistant
  // text from the start of the run on every event (see
  // backend/core/gateway/connection_pool.py:1080 "chunks use cumulative
  // replacement"). When a tool interrupts streaming we commit the in-progress
  // chatStream into chatStreamSegments; subsequent chunks for the same run
  // still arrive cumulative (pre-tool + post-tool text), so we must slice
  // off the already-committed prefix before storing the new chatStream.
  // Otherwise the segment + tail would both contain the pre-tool text and
  // the synthesized assistant message at `done` would duplicate it.
  const streamCommittedOffsetRef = useRef<number>(0);
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
  // Ref mirrors state so the once-registered onChatMessage handler can read
  // the live value without stale-closure bugs (same pattern as chatStream).
  const [chatStreamSegments, _setChatStreamSegmentsRaw] = useState<Array<{ text: string; ts: number }>>([]);
  const chatStreamSegmentsRef = useRef<Array<{ text: string; ts: number }>>([]);
  const setChatStreamSegments = useCallback(
    (updater: Array<{ text: string; ts: number }> | ((prev: Array<{ text: string; ts: number }>) => Array<{ text: string; ts: number }>)) => {
      _setChatStreamSegmentsRaw((prev) => {
        const next = typeof updater === "function" ? updater(prev) : updater;
        chatStreamSegmentsRef.current = next;
        return next;
      });
    },
    [],
  );

  // Live tool state.
  const [chatToolMessages, _setChatToolMessagesRaw] = useState<HistoryMessage[]>([]);
  const chatToolMessagesRef = useRef<HistoryMessage[]>([]);
  const setChatToolMessages = useCallback((next: HistoryMessage[]) => {
    chatToolMessagesRef.current = next;
    _setChatToolMessagesRaw(next);
  }, []);
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

  // Analytics counters.
  const turnStartedAtRef = useRef<number | null>(null);
  const turnAssistantLenRef = useRef<number>(0);

  // RunIds the user has explicitly aborted/cleared. Late chunk/thinking/
  // tool_start/tool_end events for these runs are dropped to prevent
  // ghost-bubble regressions (the previous getOrCreateBubble guard
  // protected against this implicitly).
  //
  // Entries are removed when the matching `error` arrives (chat.aborted →
  // error transform per connection_pool.py) — that's the cleanup point for
  // an aborted run.
  const cancelledRunIdsRef = useRef<Set<string>>(new Set());

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
            // Emit tool_result on terminal state regardless of payload —
            // see foldLiveTurnIntoContent for rationale.
            ...(e.status !== "running"
              ? [
                  {
                    type: "tool_result",
                    tool_use_id: e.toolCallId,
                    content: e.output ?? "",
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
    // Force-flush immediately if any entry is in terminal state — this must
    // run BEFORE the "already scheduled" early-return, otherwise a tool_end
    // arriving while a throttle timer is pending gets stuck behind the timer
    // (terminal-state tool_result lands ~80ms late and the UI flashes a
    // half-finished tool card).
    const hasTerminal = Array.from(toolStreamByIdRef.current.values()).some(
      (e) => e.status !== "running",
    );
    if (hasTerminal) {
      if (toolStreamSyncTimerRef.current !== null) {
        window.clearTimeout(toolStreamSyncTimerRef.current);
        toolStreamSyncTimerRef.current = null;
      }
      flush();
      return;
    }
    if (toolStreamSyncTimerRef.current !== null) {
      return; // already scheduled
    }
    toolStreamSyncTimerRef.current = window.setTimeout(flush, TOOL_STREAM_THROTTLE_MS);
  }, [setChatToolMessages]);

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

      // Drop late stream/tool events for a runId the user already aborted
      // or cleared. Without this, an event landing after cancelMessage()
      // (e.g. a chunk that was in flight when the user hit Stop) would
      // reanimate live state and flip isStreaming back on. `done`/`error`
      // are allowed through so they can clean up the cancelled-runIds set.
      if (
        (msg.type === "chunk" ||
          msg.type === "thinking" ||
          msg.type === "tool_start" ||
          msg.type === "tool_end" ||
          msg.type === "tool_error") &&
        cancelledRunIdsRef.current.has(msg.runId)
      ) {
        return;
      }

      if (msg.type === "chunk") {
        const runId = msg.runId;
        // Run boundary: either first event of a fresh turn (current === null)
        // or a new sub-run (current !== runId). Commit any in-progress stream
        // as a segment and reset the cumulative-replacement offset so the
        // new run's chunks start at slice(0).
        if (chatStreamRunIdRef.current === null || chatStreamRunIdRef.current !== runId) {
          const prior = chatStreamRef.current;
          if (prior && prior.length > 0) {
            setChatStreamSegments((segs) => [...segs, { text: prior, ts: Date.now() }]);
          }
          streamCommittedOffsetRef.current = 0;
        }
        // Slice off the already-committed prefix; msg.content is cumulative
        // from the start of the run.
        const tail = msg.content.slice(streamCommittedOffsetRef.current);
        setChatStream(tail.length > 0 ? tail : null);
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

        // Always push a segment (possibly empty-text) so chatStreamSegments[i]
        // stays index-aligned with chatToolMessages[i] — buildChatItems and
        // foldLiveTurnIntoContent both rely on the contract that segments[i]
        // is "the text immediately before tool[i]". Without the sentinel push
        // a tool firing before any text would shift post-tool text into
        // segment[0], making interleave emit it BEFORE its tool (Codex P2:
        // tool-first ordering). buildChatItems/fold already skip empty
        // segments so the sentinel is invisible at render time.
        //
        // Advance streamCommittedOffsetRef by the committed length so the
        // next post-tool chunk (which arrives cumulative from start-of-run)
        // slices off the already-committed prefix. Do NOT clear
        // chatStreamRunIdRef — the same run continues after the tool returns.
        const priorStream = chatStreamRef.current ?? "";
        setChatStreamSegments((segs) => [...segs, { text: priorStream, ts: Date.now() }]);
        if (priorStream.length > 0) {
          streamCommittedOffsetRef.current += priorStream.length;
          setChatStream(null);
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
        // Fold the entire live turn into a single synthesized assistant
        // message so chronological order is preserved across turns. Leaving
        // segments/tools in the live arrays would cause them to render
        // AFTER the next user message (cross-turn ordering bug).
        const segments = chatStreamSegmentsRef.current;
        const tailStream = chatStreamRef.current;
        const tailThinking = chatStreamThinkingRef.current;
        const content = foldLiveTurnIntoContent({
          segments,
          toolEntries: toolStreamByIdRef.current,
          toolOrder: toolStreamOrderRef.current,
          tailStream,
          tailThinking,
        });

        if (content.length > 0) {
          const synthesized: HistoryMessage = {
            role: "assistant",
            content,
            timestamp: Date.now(),
            runId,
          };
          setChatMessages((prev) => [...prev, synthesized]);
          const segText = segments.reduce((acc, s) => acc + s.text.length, 0);
          turnAssistantLenRef.current += segText + (tailStream?.length ?? 0);
        }

        // Clear ALL live state so the next turn starts from a blank slate.
        setChatStream(null);
        chatStreamRunIdRef.current = null;
        chatStreamStartedAtRef.current = null;
        streamCommittedOffsetRef.current = 0;
        setChatStreamThinking(null);
        chatStreamThinkingRunIdRef.current = null;
        setChatStreamSegments([]);
        toolStreamByIdRef.current.clear();
        toolStreamOrderRef.current = [];
        setChatToolMessages([]);
        if (runId) cancelledRunIdsRef.current.delete(runId);

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
        // Clear ALL live state — including tool state. Without clearing
        // tools, a tool that was still `running` when the error arrived
        // would keep the stillRunning check true forever and block
        // setIsStreaming(false), leaving the chat stuck in streaming mode.
        setChatStream(null);
        chatStreamRunIdRef.current = null;
        streamCommittedOffsetRef.current = 0;
        setChatStreamThinking(null);
        chatStreamThinkingRunIdRef.current = null;
        setChatStreamSegments([]);
        toolStreamByIdRef.current.clear();
        toolStreamOrderRef.current = [];
        setChatToolMessages([]);
        setError(displayError);
        if (!msg.runId || msg.runId === clientRunIdRef.current) {
          clientRunIdRef.current = null;
        }
        if (msg.runId) cancelledRunIdsRef.current.delete(msg.runId);
        if (clientRunIdRef.current === null) {
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

    // Remember the cancelled runId so any chunk/thinking/tool events still
    // in flight from this run get dropped instead of reanimating live state.
    // The server's chat.aborted event will eventually arrive as a terminal
    // `error` which removes the entry.
    const cancelledRunId = clientRunIdRef.current;
    if (cancelledRunId) cancelledRunIdsRef.current.add(cancelledRunId);

    const sessionKey = `agent:${agentIdRef.current}:${sessionName}`;
    try {
      await sendReq("chat.abort", { sessionKey });
    } catch (err) {
      console.warn("Failed to abort agent run:", err);
    }

    // Fold the in-progress turn into a single assistant HistoryMessage so
    // the user sees what they cancelled preserved in chat history. Without
    // this fold, the live segments/tools would render AFTER the next user
    // message at the buildChatItems tail — same cross-turn ordering bug
    // that bit the `done` path.
    const content = foldLiveTurnIntoContent({
      segments: chatStreamSegmentsRef.current,
      toolEntries: toolStreamByIdRef.current,
      toolOrder: toolStreamOrderRef.current,
      tailStream: chatStreamRef.current,
      tailThinking: chatStreamThinkingRef.current,
    });
    if (content.length > 0) {
      const synthesized: HistoryMessage = {
        role: "assistant",
        content,
        timestamp: Date.now(),
        runId: cancelledRunId ?? undefined,
      };
      setChatMessages((prev) => [...prev, synthesized]);
    }

    setIsStreaming(false);
    setChatStream(null);
    chatStreamRunIdRef.current = null;
    streamCommittedOffsetRef.current = 0;
    setChatStreamThinking(null);
    chatStreamThinkingRunIdRef.current = null;
    setChatStreamSegments([]);
    toolStreamByIdRef.current.clear();
    toolStreamOrderRef.current = [];
    setChatToolMessages([]);
    clientRunIdRef.current = null;
  }, [
    isStreaming,
    sendReq,
    sessionName,
    setIsStreaming,
    setChatStream,
    setChatStreamThinking,
    setChatStreamSegments,
    setChatToolMessages,
  ]);

  // ---- Clear messages ----

  const clearMessages = useCallback(() => {
    // Late events for the cleared run shouldn't repopulate the now-empty
    // chat — mark its runId as cancelled so the handler drops them.
    const activeRunId = clientRunIdRef.current;
    if (activeRunId) cancelledRunIdsRef.current.add(activeRunId);
    setChatMessages([]);
    setChatStream(null);
    chatStreamRunIdRef.current = null;
    streamCommittedOffsetRef.current = 0;
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
    clientRunIdRef.current = null;
  }, [
    sessionName,
    setIsStreaming,
    setChatStream,
    setChatStreamThinking,
    setChatStreamSegments,
    setChatToolMessages,
  ]);

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
