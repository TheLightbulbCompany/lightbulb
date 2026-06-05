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
import { useExecApprovalsContextOptional } from "@/hooks/useExecApprovals";
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
 * flight, OR while disconnected. Held in `queuedMessages` and drained
 * one-at-a-time off the SERVER's run-ended signal (`run_ended`, or a
 * terminal done/error for the active run when no run_ended trails it) —
 * never on a local busy→idle edge. Mirrors upstream OpenClaw control-ui's
 * chat queue (~/Desktop/openclaw/ui/src/ui/app-chat.ts:181-217).
 *
 * `sendState` tracks a send's transport status:
 *   - undefined  — a plain queued send awaiting the normal drain.
 *   - "waiting-reconnect" — enqueued/attempted while disconnected; replayed
 *     on the next reconnect rising edge.
 *   - "failed" — a dispatch attempt threw; retryable via retryQueuedMessage.
 * (`"sending"` is reserved for parity with upstream but not used here, since
 * a dispatched send becomes its own active run rather than staying queued.)
 */
export interface QueuedMessage {
  id: string;
  text: string;
  createdAt: number;
  sendState?: "sending" | "waiting-reconnect" | "failed";
  sendError?: string;
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
  steerMessage: (id: string) => void;
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
  const { isConnected, sendChat, send, onChatMessage, sendReq } = useGateway();

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

  // Busy gate. The approval-pause guard is implemented below via the
  // approvalQueueRef + queue-drain effect — OpenClaw emits chat.final
  // mid-turn while awaiting an exec approval and only starts the real
  // continuation after the user resolves it. Releasing the gate on that
  // intermediate `done` would reopen ChatInput and let a user-typed
  // follow-up race with the post-approval resume.
  const clientRunIdRef = useRef<string | null>(null);

  // Deferred abort intent. cancelMessage() while disconnected stores the
  // target {agentId, runId} here; the reconnect rising-edge effect below
  // replays it exactly once. Mirrors upstream app-chat.ts's host.pendingAbort
  // (~/Desktop/openclaw/ui/src/ui/app-chat.ts:213).
  const pendingAbortRef = useRef<{ agentId: string; runId: string | null } | null>(null);

  // Approval queue snapshot — used to keep the busy gate armed while an
  // exec-approval card is still pending for this agent. Mirrored to a ref
  // so the once-registered onChatMessage handler reads the live value.
  // We also subscribe to reason-tagged removal events: `dismissed`,
  // `auto_pruned`, and `terminal_error` release the gate (no continuation
  // is coming server-side), but `resolved` keeps it armed until the
  // post-approval continuation's terminal event arrives — otherwise a
  // follow-up typed in the window between queue-empty and the first
  // continuation chunk would race with the resume.
  const { queue: approvalQueue, onApprovalRemoved } =
    useExecApprovalsContextOptional();
  const approvalQueueRef = useRef(approvalQueue);
  useEffect(() => {
    approvalQueueRef.current = approvalQueue;
  }, [approvalQueue]);

  // After a `resolved` approval removal, the OpenClaw continuation
  // arrives on a FRESH runId (the original run truly ended at the
  // intermediate chat.final). The next chunk/thinking/tool_start we
  // see is therefore the resume — transfer the busy gate to that fresh
  // runId so its eventual terminal `done` releases the gate the normal
  // way. Without this, `done.runId !== clientRunIdRef.current` and the
  // gate stays armed forever.
  const resumeExpectedRef = useRef(false);

  // Pending message queue.
  const [queuedMessages, setQueuedMessages] = useState<QueuedMessage[]>([]);
  const queuedMessagesRef = useRef<QueuedMessage[]>([]);
  useEffect(() => {
    queuedMessagesRef.current = queuedMessages;
  }, [queuedMessages]);

  // RunIds whose queue-drain has already fired. The queue drains on the
  // SERVER's run-ended signal, which arrives as up to TWO frames for a
  // single run: the terminal done/error AND the authoritative `run_ended`
  // that trails it (run_ended is ADDITIONAL, never a replacement — see
  // backend connection_pool.py). Some session-less slash-command terminals
  // emit a `done` with NO trailing run_ended. We therefore drain on EITHER
  // signal but dedupe by runId here so done+run_ended for the same run never
  // drains twice (which would double-send the next queued message).
  const drainedRunIdsRef = useRef<Set<string>>(new Set());

  // Ref to the latest queue-drain function. The once-registered
  // onChatMessage handler (deps [onChatMessage]) reads it through this ref
  // so it can invoke the drain without re-binding on every render or
  // capturing a stale dispatchNow closure. Assigned after dispatchNow is
  // defined below.
  const drainQueueRef = useRef<() => void>(() => {});

  const removeQueuedMessage = useCallback((id: string) => {
    setQueuedMessages((prev) => prev.filter((m) => m.id !== id));
  }, []);

  const clearQueue = useCallback(() => {
    setQueuedMessages([]);
  }, []);

  // Steer a queued message INTO the live run. Only valid while a run is
  // active (clientRunIdRef set) — the steer text is injected mid-run rather
  // than queued to run after. Sends {type:"agent_steer", agent_id, text,
  // runId} via the gateway send primitive (mirroring how sendChat emits
  // agent_chat) and removes the item from the normal queue, since it's now
  // part of the live run. No-op if no active run or the id isn't queued.
  // Mirrors upstream OpenClaw web UI's steerQueuedChatMessage
  // (~/Desktop/openclaw/ui/src/ui/app-chat.ts:737).
  const steerMessage = useCallback(
    (id: string) => {
      const activeRunId = clientRunIdRef.current;
      if (!activeRunId) return;
      if (!agentIdRef.current) return;
      if (!isConnected) return;
      const item = queuedMessagesRef.current.find((m) => m.id === id);
      if (!item) return;
      const text = item.text.trim();
      if (!text) return;

      send({
        type: "agent_steer",
        agent_id: agentIdRef.current,
        text,
        runId: activeRunId,
      });
      setQueuedMessages((prev) => prev.filter((m) => m.id !== id));
    },
    [isConnected, send],
  );

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
      // reanimate live state and flip isStreaming back on.
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

      // Tombstoned terminals: a late `done` or `error` for a runId the
      // user already aborted/cleared must clean up its tombstone marker
      // without mutating the current live turn's state. Without this, a
      // late chat.aborted-as-error for runA would append a stale "Error:"
      // bubble AND wipe runB's in-flight stream/tools; a late `done` for
      // runA would fold runB's live segments+tools into a HistoryMessage
      // tagged with runA's runId.
      if (
        (msg.type === "done" || msg.type === "error") &&
        msg.runId &&
        cancelledRunIdsRef.current.has(msg.runId)
      ) {
        cancelledRunIdsRef.current.delete(msg.runId);
        return;
      }

      // Approval-resume gate transfer: after a `resolved` removal,
      // OpenClaw's continuation starts on a fresh runId. Catch the
      // first event of that fresh run and reassign clientRunIdRef so
      // the eventual terminal `done` (which carries the fresh runId)
      // releases the busy gate. Without this transfer the gate sticks
      // armed forever and queued follow-ups never flush.
      //
      // The first fresh-run event is whichever the agent produces next
      // — usually chunk/thinking, but for an approved exec it can be
      // tool_end/tool_error (the command's result forwarded by the
      // backend's tool-result transform in connection_pool.py). It can
      // also be a bare done/error if the approved tool was the only
      // thing left in the turn. So transfer on any non-heartbeat event
      // carrying a runId.
      //
      // Require an active clientRunIdRef so we don't adopt a fresh
      // runId after the user has already cancelled/cleared. Belt and
      // suspenders: cancelMessage / clearMessages also reset
      // resumeExpectedRef so a subsequent sendMessage's new runId can
      // never be stolen by a late continuation.
      if (
        resumeExpectedRef.current &&
        clientRunIdRef.current !== null &&
        msg.type !== "heartbeat" &&
        msg.type !== "update_available" &&
        msg.runId &&
        msg.runId !== clientRunIdRef.current
      ) {
        clientRunIdRef.current = msg.runId;
        resumeExpectedRef.current = false;
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
        // segment[0], making interleave emit it BEFORE its tool.
        // buildChatItems/fold already skip empty segments so the sentinel is
        // invisible at render time.
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
        if (entry) {
          entry.output = msg.result;
          entry.status = msg.type === "tool_error" ? "error" : "done";
          entry.updatedAt = Date.now();
          toolStreamByIdRef.current.set(toolCallId, entry);
          syncToolStreamMessages();
          return;
        }
        // No live entry — likely the post-approval continuation for a
        // tool whose tool_use was already folded into a synthesized
        // HistoryMessage at the interim chat.final (live state was
        // cleared there). Append the tool_result block to that message
        // so the approved command's output reaches history and the UI,
        // instead of being silently dropped.
        setChatMessages((prev) => {
          for (let i = prev.length - 1; i >= 0; i--) {
            const m = prev[i];
            if (m.role !== "assistant" || !Array.isArray(m.content)) continue;
            const hasMatch = m.content.some(
              (b) => b.type === "tool_use" && b.id === toolCallId,
            );
            if (!hasMatch) continue;
            const alreadyHasResult = m.content.some(
              (b) =>
                b.type === "tool_result" && b.tool_use_id === toolCallId,
            );
            if (alreadyHasResult) return prev;
            const resultBlock: ContentBlock = {
              type: "tool_result",
              tool_use_id: toolCallId,
              content: msg.result ?? "",
              ...(msg.type === "tool_error" ? { isError: true } : {}),
            };
            const next = [...prev];
            next[i] = { ...m, content: [...m.content, resultBlock] };
            return next;
          }
          return prev;
        });
        return;
      }

      if (msg.type === "run_ended") {
        // The AUTHORITATIVE server signal that this run is truly over. It
        // trails the terminal done/error for the same run and is the
        // canonical drain trigger — the queue drains only when the run is
        // truly over per the SERVER, never on a local terminal/error edge.
        //
        // Dedupe by runId: the terminal done/error handler below already
        // drained this run (or, for the session-less edge, a `done` with no
        // run_ended did). If so, this is a no-op so we don't double-send.
        const runId = msg.runId;
        if (drainedRunIdsRef.current.has(runId)) {
          return;
        }
        drainedRunIdsRef.current.add(runId);
        // run_ended is the server's word that the run ended — force-release
        // the busy gate for it even if some local heuristic still held it.
        if (clientRunIdRef.current === runId) {
          clientRunIdRef.current = null;
          resumeExpectedRef.current = false;
        }
        drainQueueRef.current();
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
        // Tombstone cleanup happens in the short-circuit at the top of
        // this handler; by here, msg.runId is never in the set.

        // Busy-gate clear, with approval-pause guard. OpenClaw emits
        // chat.final mid-turn when an exec tool needs approval and only
        // starts the real continuation after the user resolves the card.
        // If a pending approval for this agent is still in the queue,
        // keep the gate armed so ChatInput stays disabled — releasing
        // here would let a user-typed follow-up race with the
        // post-approval resume. The queue-drain effect below releases
        // the gate when the queue empties via resolve/dismiss/auto-prune.
        const hasPendingApprovalForThisAgent =
          approvalQueueRef.current.some(
            (a) => a.agentId === agentIdRef.current,
          );
        // Did THIS done end the active run? Only a true terminal (matching
        // runId, no pending approval) clears the gate. An interim chat.final
        // that closes a tool-approval bracket keeps the gate armed — and the
        // backend sends NO run_ended for it — so we must not drain on it.
        const endedActiveRun =
          runId === clientRunIdRef.current && !hasPendingApprovalForThisAgent;
        if (endedActiveRun) {
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

        // Drain the queue if this done genuinely ended the active run. The
        // authoritative `run_ended` trails most terminals — dedupe by runId
        // so we don't double-drain. For the session-less slash-command edge
        // (a `done` with no trailing run_ended) this `done` IS the only
        // run-ended signal, so the drain must happen here too.
        if (endedActiveRun && !drainedRunIdsRef.current.has(runId)) {
          drainedRunIdsRef.current.add(runId);
          drainQueueRef.current();
        }
        return;
      }

      if (msg.type === "error") {
        // agent_busy is a backend SAFETY-NET frame for a send that was
        // gated server-side (the agent is still working the previous turn).
        // It carries NO runId and is NOT a terminal for the live run — it's
        // a benign "still busy" notice, primarily for non-frontend senders.
        // The frontend already enqueues sends while busy, so for the live
        // turn this is informational only. Branch on it FIRST: the generic
        // error path below would (because !msg.runId) clear clientRunIdRef
        // and isStreaming, WIPING the in-flight turn's streaming state.
        if (msg.code === "agent_busy") {
          return;
        }

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
        // Did THIS error end the active run? A terminal agent error carries
        // the run's runId (the no-runId agent_busy notice was handled and
        // returned above). Capture the match before clearing so we can drain.
        const endedActiveRun =
          !!msg.runId && msg.runId === clientRunIdRef.current;
        if (!msg.runId || msg.runId === clientRunIdRef.current) {
          clientRunIdRef.current = null;
        }
        // Tombstone cleanup happens in the short-circuit at the top of
        // this handler; by here, msg.runId is never in the set.
        if (clientRunIdRef.current === null) {
          setIsStreaming(false);
        }

        // Drain the queue if this error genuinely ended the active run. A
        // trailing `run_ended {outcome:"error"}` is deduped by runId so the
        // pair never double-drains. (No-runId transport errors don't drain
        // — there's no run-ended runId to key the drain/dedupe on.)
        if (endedActiveRun && !drainedRunIdsRef.current.has(msg.runId!)) {
          drainedRunIdsRef.current.add(msg.runId!);
          drainQueueRef.current();
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

  // Approval-pause release / resume signaling.
  //
  // `resolved`: a continuation is coming on a fresh runId — arm the
  //   resumeExpectedRef flag so the chunk/thinking/tool_start at the top
  //   of onChatMessage transfers the busy gate. Don't touch the gate
  //   here; releasing it before the continuation arrives would let a
  //   user follow-up race with the resume.
  //
  // `dismissed` / `auto_pruned` / `terminal_error`: no server-side
  //   continuation is coming. Release the gate AND drop isStreaming —
  //   the interim `done` handler skipped setIsStreaming(false) because
  //   the gate was held for the approval, so the chat would otherwise
  //   show the stop button indefinitely.
  useEffect(() => {
    return onApprovalRemoved((removedId, reason) => {
      // Subscribers fire synchronously inside removeFromQueue before
      // React commits the setQueue update, so approvalQueueRef.current
      // still holds the pre-removal queue and we can look up the
      // removed id's agentId here.
      const wasForThisAgent = approvalQueueRef.current.some(
        (a) => a.id === removedId && a.agentId === agentIdRef.current,
      );
      if (!wasForThisAgent) return;

      if (reason === "resolved") {
        if (clientRunIdRef.current !== null) {
          resumeExpectedRef.current = true;
        }
        return;
      }

      if (clientRunIdRef.current === null) return;
      // Multiple approvals can be pending for a single run (parallel
      // tools each requesting one). Removing ONE via dismiss/expiry/
      // terminal-error must not release the gate while siblings are
      // still queued — the agent is still effectively paused on them.
      const siblingPending = approvalQueueRef.current.some(
        (a) => a.id !== removedId && a.agentId === agentIdRef.current,
      );
      if (siblingPending) return;
      const hasLiveStream =
        chatStreamRef.current !== null || toolStreamByIdRef.current.size > 0;
      if (!hasLiveStream) {
        clientRunIdRef.current = null;
        resumeExpectedRef.current = false;
        if (isStreamingRef.current) {
          setIsStreaming(false);
        }
        // Drop any half-set turn analytics state — the turn ended
        // abruptly (no chat_completed emission), so we just clear the
        // refs so the NEXT user turn measures from its own start.
        turnStartedAtRef.current = null;
        turnAssistantLenRef.current = 0;
        // The gate just released with no server run-ended frame coming
        // (the user dismissed / the card expired / it errored — there's no
        // post-approval continuation). Drain the queue here explicitly since
        // the server-signal handlers won't fire for this locally-resolved
        // termination.
        drainQueueRef.current();
      }
    });
  }, [onApprovalRemoved, setIsStreaming]);

  // Drain ONE queued message if the run is truly over and we're not busy.
  //
  // This is invoked off the SERVER's run-ended signal — `run_ended`, or a
  // terminal done/error for the active runId when no run_ended trails it —
  // NEVER on a local clientRunId falling edge. Mirrors upstream OpenClaw web
  // UI's flushChatQueue (app-chat.ts:775): drain only when !isChatBusy, send
  // one, and let the dispatched message's own run-ended re-invoke the drain
  // for the next one. dispatchNow re-arms clientRunIdRef, so the gate stays
  // closed for the freshly-dispatched run until its server terminal.
  const drainQueue = useCallback(() => {
    if (clientRunIdRef.current !== null) return; // still busy
    if (!isConnected) return;
    if (queuedMessagesRef.current.length === 0) return;
    const next = queuedMessagesRef.current[0];
    setQueuedMessages((q) => q.slice(1));
    void dispatchNow(next.text);
  }, [isConnected, dispatchNow]);

  // Keep the handler-visible drain ref pointed at the latest drainQueue so
  // the once-registered onChatMessage handler invokes the current closure
  // (fresh isConnected / dispatchNow) without re-binding.
  useEffect(() => {
    drainQueueRef.current = drainQueue;
  }, [drainQueue]);

  const sendMessage = useCallback(
    async (message: string): Promise<void> => {
      if (!agentIdRef.current) {
        throw new Error("No agent selected");
      }

      const trimmed = message.trim();
      if (!trimmed) return;

      setError(null);
      const key = agentIdRef.current ? `${agentIdRef.current}:${sessionName}` : null;
      if (key) _needsBootstrap.delete(key);

      // Disconnected: park the send as waiting-reconnect instead of
      // dropping it. The reconnect rising-edge effect replays it (and
      // resumes the normal drain) once the socket is back. Mirrors upstream
      // markQueuedChatSendsWaitingForReconnect (chat-queue.ts:848).
      if (!isConnected) {
        const id = `queued-${crypto.randomUUID()}`;
        setQueuedMessages((prev) => [
          ...prev,
          {
            id,
            text: trimmed,
            createdAt: Date.now(),
            sendState: "waiting-reconnect",
          },
        ]);
        return;
      }

      // Busy gate: if a user-turn is already in flight (clientRunIdRef is
      // set), enqueue instead of firing chat.send. The queue drains off the
      // server's run-ended signal for the active run (see drainQueue).
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
  //
  // Targeted, server-reconciled abort. We send `agent_abort` carrying the
  // active clientRunId and then WAIT for the server's terminal — the aborted
  // run surfaces as an `error` ("Agent run was cancelled" per
  // connection_pool.py's chat.aborted transform) followed by the
  // authoritative `run_ended {outcome:"aborted"}`. Those handlers tear down
  // the live turn and drain the queue. We deliberately do NOT clear
  // isStreaming / live state locally: server-reconcile drives "done", not a
  // local edge. Mirrors upstream OpenClaw web UI's handleAbortChat
  // (~/Desktop/openclaw/ui/src/ui/app-chat.ts:201).
  const cancelMessage = useCallback(async () => {
    if (!agentIdRef.current || !isStreaming) return;

    const startedAt = turnStartedAtRef.current;
    capture("chat_aborted", {
      agent_id: agentIdRef.current,
      elapsed_ms: startedAt !== null ? Date.now() - startedAt : 0,
    });

    const activeRunId = clientRunIdRef.current;

    // Disconnected: queue the abort intent and replay it on the next
    // reconnect rising edge (see the pending-abort replay effect below).
    // Mirrors upstream app-chat.ts:211.
    if (!isConnected) {
      pendingAbortRef.current = {
        agentId: agentIdRef.current,
        runId: activeRunId,
      };
      return;
    }

    send({
      type: "agent_abort",
      agent_id: agentIdRef.current,
      ...(activeRunId ? { runId: activeRunId } : {}),
    });
  }, [isStreaming, isConnected, send]);

  // ---- Reconnect rising-edge: replay deferred abort + waiting sends ----
  //
  // On the disconnected→connected transition:
  //   1. Fire a deferred abort parked by cancelMessage() (pendingAbortRef),
  //      exactly once. Mirrors app-chat.ts's host.pendingAbort handling.
  //   2. Replay any queued sends marked `waiting-reconnect` — strip the
  //      flag so they become plain queued sends, then resume the normal
  //      drain (drainQueue dispatches the head; each dispatched send's
  //      server run-ended re-invokes the drain for the next). Mirrors
  //      retryReconnectableQueuedChatSends (chat-queue.ts:883).
  const prevConnectedRef = useRef(isConnected);
  useEffect(() => {
    const rose = !prevConnectedRef.current && isConnected;
    prevConnectedRef.current = isConnected;
    if (!rose) return;

    const pending = pendingAbortRef.current;
    if (pending) {
      pendingAbortRef.current = null;
      send({
        type: "agent_abort",
        agent_id: pending.agentId,
        ...(pending.runId ? { runId: pending.runId } : {}),
      });
    }

    const hasWaiting = queuedMessagesRef.current.some(
      (m) => m.sendState === "waiting-reconnect",
    );
    if (hasWaiting) {
      // Strip the waiting-reconnect flag so the items become plain queued
      // sends (UI no longer shows "Waiting for reconnect").
      setQueuedMessages((prev) =>
        prev.map((m) =>
          m.sendState === "waiting-reconnect"
            ? { ...m, sendState: undefined, sendError: undefined }
            : m,
        ),
      );
      // Resume the normal drain. drainQueue reads the head off
      // queuedMessagesRef (the text is unchanged by the flag strip) and
      // slices it via a functional setState updater, so it composes
      // correctly with the strip above regardless of commit ordering.
      drainQueueRef.current();
    }
  }, [isConnected, send]);

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
    // Same reasoning as cancelMessage — a post-approval continuation
    // must not be adopted as the new active run after a clear.
    resumeExpectedRef.current = false;
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
    steerMessage,
  };
}
