// frontend/src/hooks/useAgentChat.ts
/**
 * Agent chat hook that uses the shared GatewayProvider WebSocket.
 *
 * Message protocol (unchanged):
 * - Send: { type: "agent_chat", agent_id: string, message: string }
 * - Receive: { type: "chunk", content: string }
 * - Receive: { type: "done" }
 * - Receive: { type: "error", message: string }
 * - Receive: { type: "heartbeat" }
 */

"use client";

import { useState, useCallback, useRef, useEffect, useMemo } from "react";
import {
  useGateway,
  type ChatIncomingMessage,
} from "@/hooks/useGateway";
import type { ToolUse } from "@/components/chat/MessageList";
import { capture } from "@/lib/analytics";

// Re-export ToolUse so existing consumers (AgentChatWindow) keep working
// after this hook switched to the canonical MessageList definition.
export type { ToolUse } from "@/components/chat/MessageList";

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
// Content extraction
// =============================================================================

/**
 * Extract text from OpenClaw message content blocks.
 * chat.history returns content as an array of blocks: [{ type: "text", text: "..." }, ...]
 */
interface ContentBlock {
  type: string;
  text?: string;
  thinking?: string;
}

function extractTextContent(content: ContentBlock[]): string {
  return content
    .filter((block) => block.type === "text" || block.type === "output_text" || block.type === "input_text")
    .map((block) => block.text ?? "")
    .join("");
}

function extractThinkingContent(content: ContentBlock[]): string {
  return content
    .filter((block) => block.type === "thinking")
    .map((block) => block.thinking ?? block.text ?? "")
    .join("\n\n");
}

// =============================================================================
// Types
// =============================================================================

export interface AgentMessage {
  role: "user" | "assistant";
  content: string;
  thinking?: string;
  toolUses?: ToolUse[];
  /**
   * Set when the assistant bubble represents a terminal agent error. The
   * value is OpenClaw's `errorKind` (refusal | timeout | rate_limit |
   * context_length | unknown) forwarded by the backend. MessageList uses
   * this to render a typed error card with tailored copy / actions.
   * Undefined for normal assistant bubbles.
   */
  errorCode?: string;
}

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

export const BOOTSTRAP_MESSAGE =
  "Hello! Please run Bootstrap.md — I'd love to learn about everyday life use cases where you can be helpful and automate work for me.";

export interface UseAgentChatReturn {
  messages: AgentMessage[];
  isStreaming: boolean;
  error: string | null;
  sendMessage: (message: string) => Promise<void>;
  cancelMessage: () => Promise<void>;
  clearMessages: () => void;
  isConnected: boolean;
  isLoadingHistory: boolean;
  needsBootstrap: boolean;
  // ↓ new: chat-queue surface, mirrors upstream OpenClaw control-ui
  // (~/Desktop/openclaw/ui/src/ui/app-chat.ts:181-217,420-426).
  queuedMessages: QueuedMessage[];
  removeQueuedMessage: (id: string) => void;
  clearQueue: () => void;
}

interface InternalMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  thinking?: string;
  toolUses?: ToolUse[];
  errorCode?: string;
}

// =============================================================================
// Module-level message cache
//
// Survives component unmounts (navigation away from /chat and back) but
// clears on full page refresh. Keyed by agentId so each agent's history
// is preserved independently.
// =============================================================================

const _messageCache = new Map<string, InternalMessage[]>();
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
  // onEvent is intentionally not destructured here — exec.approval.* is now
  // owned by useExecApprovals at the chat-shell level (see commit message
  // for the previous "Approval handling moved out of useAgentChat" comment).
  const { isConnected, sendChat, onChatMessage, sendReq } = useGateway();

  // Cache key includes session name so org members don't share history cache
  const cacheKey = agentId ? `${agentId}:${sessionName}` : null;

  const [messages, setMessages] = useState<InternalMessage[]>(
    () => (cacheKey ? _messageCache.get(cacheKey) ?? [] : []),
  );
  const [isStreaming, _setIsStreamingRaw] = useState(false);
  const isStreamingRef = useRef(false);
  const setIsStreaming = useCallback((value: boolean) => {
    isStreamingRef.current = value;
    _setIsStreamingRaw(value);
  }, []);
  const [error, setError] = useState<string | null>(null);
  const [historyLoadState, setHistoryLoadState] = useState<"idle" | "loading" | "done">("idle");
  const isLoadingHistory = historyLoadState === "loading";

  // Multi-bubble: one assistant message per OpenClaw runId. OpenClaw assigns
  // a new runId per LLM turn within a single chat.send; each turn gets its
  // own bubble.
  // See docs/superpowers/specs/2026-04-21-multi-bubble-chat-design.md.
  type RunState = {
    messageId: string;
    streamContent: string;
    thinking: string;
  };
  const runsRef = useRef<Map<string, RunState>>(new Map());

  // Tracks runIds that have been finalized (done / error / budget / cancel /
  // clear / agent-drop). Any late chunk/thinking/tool_start event arriving for
  // one of these runIds is silently dropped by getOrCreateBubble — without
  // this, such a late event would create a brand-new empty assistant bubble
  // and flip isStreaming back to true (ghost-bubble regression, M1).
  //
  // Bounded to ~100 entries via FIFO eviction below; the first-inserted runId
  // is evicted when the set would otherwise grow past the cap. This avoids
  // unbounded growth over long sessions while still covering the protocol's
  // realistic out-of-order window (events straggle within a single turn, not
  // across dozens of turns).
  const finalizedRunsRef = useRef<Set<string>>(new Set());

  // When cancelMessage/clearMessages fires before any event has arrived for
  // the in-flight turn, we don't know what runId the server will use — so
  // we can't pre-populate finalizedRunsRef. This flag closes the window:
  // any unknown runId arriving while pending-cancel is set is treated as
  // the cancelled turn and silently dropped. Cleared on the next
  // sendMessage.
  const pendingCancelRef = useRef<boolean>(false);

  // Analytics: wall-clock when the user hit send (one per turn). Used to
  // compute duration_ms for the `chat_completed` event. Cleared on the
  // terminal event for the turn (all runs finalized, cancel, or error).
  const turnStartedAtRef = useRef<number | null>(null);
  // Accumulates assistant content length across all runs in the current
  // turn — multiple runIds can stream in a single chat.send when the agent
  // chains sub-turns. We sum the FINAL streamContent of each run when it
  // finalizes so `assistant_message_length` reflects what the user saw.
  const turnAssistantLenRef = useRef<number>(0);

  // Hoisted above finalizeBubble so the turn-complete analytics capture
  // can read the current agent id without TS "used before declaration"
  // complaints. Kept in sync by the effect further down.
  const agentIdRef = useRef(agentId);

  // Upstream pattern: one client-assigned UUID per user-initiated chat.send.
  // Cleared only on the chat.final / aborted / error event whose runId
  // matches this UUID. Distinct from runsRef (per-bubble) — they are
  // orthogonal: bubbles are for display, this flag is for send gating.
  // See ~/Desktop/openclaw/ui/src/ui/controllers/chat.ts:627-770.
  const clientRunIdRef = useRef<string | null>(null);

  // Frontend chat queue: when the user types while clientRunIdRef.current
  // is set (user-turn in progress), new sends push here instead of firing
  // immediately. Drained one-at-a-time on the busy→idle transition. Mirrors
  // ~/Desktop/openclaw/ui/src/ui/app-chat.ts:181-217,388-417.
  const [queuedMessages, setQueuedMessages] = useState<QueuedMessage[]>([]);
  const queuedMessagesRef = useRef<QueuedMessage[]>([]);
  // Keep the ref in sync so setMessages-style updates can read fresh state
  // from inside event handler callbacks without stale-closure bugs.
  useEffect(() => {
    queuedMessagesRef.current = queuedMessages;
  }, [queuedMessages]);

  const removeQueuedMessage = useCallback((id: string) => {
    setQueuedMessages((prev) => prev.filter((m) => m.id !== id));
  }, []);

  const clearQueue = useCallback(() => {
    setQueuedMessages([]);
  }, []);

  const getOrCreateBubble = useCallback((runId: string): string | null => {
    // Cancel-before-first-event race: if the user hit Stop/Clear BEFORE any
    // chunk/thinking/tool_start arrived for the in-flight turn, runsRef was
    // empty — so finalizeAllActiveRuns had no runIds to tombstone. A late
    // first event for that cancelled turn would otherwise fall through to
    // bubble-creation below, spawning a ghost bubble and flipping
    // isStreaming back to true (silently undoing the cancel). Treat any
    // unknown runId arriving during the pending-cancel window as the
    // cancelled turn and drop it.
    const existing = runsRef.current.get(runId);
    if (!existing && pendingCancelRef.current) {
      finalizedRunsRef.current.add(runId);
      if (finalizedRunsRef.current.size > 100) {
        const first = finalizedRunsRef.current.values().next().value;
        if (first) finalizedRunsRef.current.delete(first);
      }
      return null;
    }

    // Late event for an already-finalized run — drop silently. This
    // replicates the old `if (!currentAssistantIdRef.current) return;`
    // blanket guard, but scoped to truly-finalized runs instead of the
    // global streaming flag.
    if (finalizedRunsRef.current.has(runId)) {
      return null;
    }

    if (existing) return existing.messageId;

    const messageId = `assistant-${crypto.randomUUID()}`;
    runsRef.current.set(runId, { messageId, streamContent: "", thinking: "" });
    setMessages((prev) => [
      ...prev,
      { id: messageId, role: "assistant", content: "" },
    ]);
    if (!isStreamingRef.current) {
      setIsStreaming(true);
    }
    return messageId;
  }, [setIsStreaming]);

  const finalizeBubble = useCallback((runId: string, options?: { errored?: boolean }) => {
    // Accumulate this run's final assistant content length into the
    // per-turn counter before we drop the run — the streamContent on the
    // RunState is the last chunk we received (chunks are replacements,
    // not deltas), so its length is the final bubble size.
    const run = runsRef.current.get(runId);
    if (run) {
      turnAssistantLenRef.current += run.streamContent.length;
    }
    runsRef.current.delete(runId);
    finalizedRunsRef.current.add(runId);
    // Bounded LRU-ish: cap size to avoid unbounded growth on long sessions.
    // Set iteration order is insertion order, so values().next() gives the
    // oldest entry.
    if (finalizedRunsRef.current.size > 100) {
      const first = finalizedRunsRef.current.values().next().value;
      if (first) finalizedRunsRef.current.delete(first);
    }
    if (runsRef.current.size === 0) {
      setIsStreaming(false);
      // Skip chat_completed on error paths (Codex P2 on PR #383): scoped
      // errors call this to clear the bubble, but a failed turn should NOT
      // be recorded as `chat_completed`. Reset the per-turn counters so a
      // late successful `done` arriving after the error doesn't emit either.
      if (options?.errored) {
        turnStartedAtRef.current = null;
        turnAssistantLenRef.current = 0;
        return;
      }
      // Turn is fully complete — emit `chat_completed` exactly once per
      // user-initiated turn. turnStartedAtRef is null for late-arriving
      // events after cancel/clear, in which case we skip the event.
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
  }, [setIsStreaming]);

  // Mark every currently-active run as finalized, then clear runsRef. Used by
  // cancel / clear / global-error branches: wiping runsRef without also
  // populating finalizedRunsRef would let a late in-flight chunk/thinking/
  // tool_start event slip past getOrCreateBubble (it wouldn't find the runId
  // in either ref) and spawn a ghost assistant bubble that re-enables
  // isStreaming — silently undoing the cancel/clear. Never clear
  // finalizedRunsRef for the same reason; rely on the 100-entry FIFO cap in
  // finalizeBubble to bound growth.
  const finalizeAllActiveRuns = useCallback(() => {
    for (const runId of runsRef.current.keys()) {
      finalizedRunsRef.current.add(runId);
      if (finalizedRunsRef.current.size > 100) {
        const first = finalizedRunsRef.current.values().next().value;
        if (first) finalizedRunsRef.current.delete(first);
      }
    }
    runsRef.current.clear();
    // Wipe the per-turn analytics state too. This path runs on terminal
    // errors (global error, budget exceeded, cancel, clear) where
    // `chat_completed` should NOT fire — clearing turnStartedAtRef here
    // is belt-and-braces against a late `done` event sneaking in and
    // firing a spurious completion capture.
    turnStartedAtRef.current = null;
    turnAssistantLenRef.current = 0;
  }, []);

  useEffect(() => {
    agentIdRef.current = agentId;
  }, [agentId]);

  // ---- Sync messages to module-level cache ----
  useEffect(() => {
    if (cacheKey && messages.length > 0) {
      _messageCache.set(cacheKey, messages);
    }
  }, [cacheKey, messages]);

  // ---- Fetch history on mount / agent change ----
  const historyLoadedRef = useRef<Set<string>>(new Set());

  useEffect(() => {
    if (!agentId || !cacheKey || !isConnected) return;
    if (historyLoadedRef.current.has(cacheKey)) return;
    // Don't fetch if we already have cached messages
    if (_messageCache.has(cacheKey)) {
      historyLoadedRef.current.add(cacheKey);
      return;
    }

    // Session key must match the backend's convention (websocket_chat.py:588):
    // all users get agent:{agentId}:{userId} to isolate from cron/system activity
    const sessionKey = `agent:${agentId}:${sessionName}`;

    // Use Promise.resolve to move setState into a callback (satisfies react-hooks/set-state-in-effect)
    Promise.resolve().then(() => setHistoryLoadState("loading"));

    sendReq("chat.history", { sessionKey, limit: 200 })
      .then((result: unknown) => {
        const historyResult = result as {
          messages?: Array<{ role: string; content: Array<{ type: string; text?: string }> }>;
        };
        historyLoadedRef.current.add(cacheKey);
        setHistoryLoadState("done");

        if (!historyResult?.messages?.length) {
          // No history = first time. Flag for bootstrap suggestion in chat input.
          _needsBootstrap.add(cacheKey);
          return;
        }

        if (agentIdRef.current !== agentId) return; // agent changed during fetch

        const loaded: InternalMessage[] = historyResult.messages
          .filter((m: { role: string }) => m.role === "user" || m.role === "assistant")
          .map((m: { role: string; content: ContentBlock[] }, i: number) => {
            const thinking = extractThinkingContent(m.content);
            return {
              id: `history-${i}`,
              role: m.role as "user" | "assistant",
              content: extractTextContent(m.content),
              ...(thinking ? { thinking } : {}),
            };
          })
          .filter((m) => m.content.length > 0);

        if (loaded.length > 0) {
          setMessages(loaded);
          _messageCache.set(cacheKey, loaded);
        }
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
      // Drop messages meant for a different agent. Heartbeat and
      // update_available aren't tied to a specific run. Errors are
      // allowed through even without agent_id as a safety valve —
      // some failure paths (client-side validation, etc.) have no
      // agent context but still need to clear the streaming state.
      const isUntaggedBroadcast =
        msg.type === "heartbeat" || msg.type === "update_available";
      const isUntaggedError = msg.type === "error" && msg.agent_id === undefined;
      if (!isUntaggedBroadcast && !isUntaggedError && msg.agent_id !== agentIdRef.current) {
        return;
      }

      if (msg.type === "chunk") {
        const runId = msg.runId;
        const messageId = getOrCreateBubble(runId);
        // Late event for a finalized run — drop.
        if (!messageId) return;
        const run = runsRef.current.get(runId)!;
        run.streamContent = msg.content;
        const updatedContent = run.streamContent;
        setMessages((prev) =>
          prev.map((m) =>
            m.id === messageId ? { ...m, content: updatedContent } : m,
          ),
        );
        return;
      }

      if (msg.type === "done") {
        const runId = msg.runId;
        finalizeBubble(runId);
        // Upstream pattern: only clear the user-turn busy flag when the
        // terminal event matches OUR clientRunId. Sub-run dones (different
        // runId) only finalize their bubble. See
        // ~/Desktop/openclaw/ui/src/ui/controllers/chat.ts:769-770.
        if (runId === clientRunIdRef.current) {
          clientRunIdRef.current = null;
        }
        return;
      }

      if (msg.type === "error") {
        const displayError = friendlyError(msg.message);
        const errorCode = msg.code;

        if (msg.runId) {
          // Scoped error: finalize that bubble only. Other runs continue.
          const run = runsRef.current.get(msg.runId);
          if (run) {
            const messageId = run.messageId;
            setMessages((prev) =>
              prev.map((m) =>
                m.id === messageId
                  ? { ...m, content: `Error: ${displayError}`, errorCode }
                  : m,
              ),
            );
            finalizeBubble(msg.runId, { errored: true });
          } else {
            // Unknown runId: the error fired before any chunk/thinking/
            // tool_start arrived, so no bubble exists yet. Append a visible
            // error-marked assistant message so the user sees the failure
            // (same UX as the sendMessage catch branch below), and clear
            // the streaming flag if no other runs are active — otherwise
            // sendMessage's early setIsStreaming(true) leaves the input
            // stuck in "Stop" mode (Codex P1 / S3).
            const errMsgId = `assistant-${crypto.randomUUID()}`;
            setMessages((prev) => [
              ...prev,
              {
                id: errMsgId,
                role: "assistant",
                content: `Error: ${displayError}`,
                errorCode,
              },
            ]);
            // Remember this runId as finalized so any late straggler events
            // for it don't create a ghost bubble.
            finalizedRunsRef.current.add(msg.runId);
            if (finalizedRunsRef.current.size > 100) {
              const first = finalizedRunsRef.current.values().next().value;
              if (first) finalizedRunsRef.current.delete(first);
            }
            if (runsRef.current.size === 0) {
              setIsStreaming(false);
              // Codex P2 (PR #383 fix follow-up): the unknown-run error
              // branch must also reset per-turn analytics counters. If a
              // later `done` arrives for an unrelated late run, finalizeBubble
              // would otherwise see runsRef.size===0 + a populated
              // turnStartedAtRef and emit a bogus chat_completed for what
              // was actually an errored turn.
              turnStartedAtRef.current = null;
              turnAssistantLenRef.current = 0;
            }
          }
          setError(displayError);
          // Clear the busy gate if this error pertains to the user's turn.
          // Scoped error with matching runId, or global error (no runId)
          // both terminate the user turn.
          if (!msg.runId || msg.runId === clientRunIdRef.current) {
            clientRunIdRef.current = null;
          }
          return;
        }

        // Global error (no runId): mark every active bubble and clear state.
        const activeMessageIds = new Set(
          Array.from(runsRef.current.values()).map((r) => r.messageId),
        );
        setMessages((prev) =>
          prev.map((m) =>
            activeMessageIds.has(m.id)
              ? { ...m, content: `Error: ${displayError}`, errorCode }
              : m,
          ),
        );
        setError(displayError);
        finalizeAllActiveRuns();
        // Close the window for late stragglers whose runId we never saw.
        pendingCancelRef.current = true;
        setIsStreaming(false);
        // Clear the busy gate if this error pertains to the user's turn.
        // Scoped error with matching runId, or global error (no runId)
        // both terminate the user turn.
        if (!msg.runId || msg.runId === clientRunIdRef.current) {
          clientRunIdRef.current = null;
        }
        return;
      }

      if (msg.type === "thinking") {
        const runId = msg.runId;
        const messageId = getOrCreateBubble(runId);
        // Late event for a finalized run — drop.
        if (!messageId) return;
        const run = runsRef.current.get(runId)!;
        run.thinking = msg.content;
        const updatedThinking = run.thinking;
        setMessages((prev) =>
          prev.map((m) =>
            m.id === messageId ? { ...m, thinking: updatedThinking } : m,
          ),
        );
        return;
      }

      if (msg.type === "tool_start") {
        const runId = msg.runId;
        const messageId = getOrCreateBubble(runId);
        // Late event for a finalized run — drop.
        if (!messageId) return;
        setMessages((prev) =>
          prev.map((m) => {
            if (m.id !== messageId) return m;
            const existing = m.toolUses ?? [];
            const existingIdx =
              msg.toolCallId !== undefined
                ? existing.findIndex((t) => t.toolCallId === msg.toolCallId)
                : -1;
            if (existingIdx >= 0) {
              const next = existing.slice();
              const prior = next[existingIdx];
              next[existingIdx] = {
                ...prior,
                tool: msg.tool,
                status: prior.status ?? ("running" as const),
                ...(msg.args ? { args: msg.args } : {}),
              };
              return { ...m, toolUses: next };
            }
            return {
              ...m,
              toolUses: [
                ...existing,
                {
                  tool: msg.tool,
                  toolCallId: msg.toolCallId,
                  status: "running" as const,
                  ...(msg.args ? { args: msg.args } : {}),
                },
              ],
            };
          }),
        );
        return;
      }

      if (msg.type === "tool_end" || msg.type === "tool_error") {
        const runId = msg.runId;
        const run = runsRef.current.get(runId);
        const messageId = run?.messageId;
        const nextStatus = msg.type === "tool_end" ? "done" : "error";

        if (!messageId) {
          // No active run for this runId — the bubble was already finalized
          // (late tool_end after chat.final, or error-scoped-unknown-run).
          // Fall back to a toolCallId scan across all assistant messages so
          // the toolCall doesn't stay stuck at status "running" forever (S2).
          if (msg.toolCallId !== undefined) {
            const targetCallId = msg.toolCallId;
            setMessages((prev) => {
              let matched = false;
              const next = prev.map((m) => {
                if (m.role !== "assistant") return m;
                const toolUses = m.toolUses ?? [];
                const idx = toolUses.findIndex(
                  (t) => t.toolCallId === targetCallId && t.status === "running",
                );
                if (idx < 0) return m;
                matched = true;
                const updated = toolUses.slice();
                updated[idx] = {
                  ...updated[idx],
                  status: nextStatus as "done" | "error",
                  ...(msg.result ? { result: msg.result } : {}),
                  ...(msg.meta ? { meta: msg.meta } : {}),
                };
                return { ...m, toolUses: updated };
              });
              return matched ? next : prev;
            });
          }
          // If no toolCallId or no match, silently drop (same as before).
          return;
        }
        setMessages((prev) =>
          prev.map((m) => {
            if (m.id !== messageId) return m;
            const toolUses = (m.toolUses || []).map((t) => {
              const matchesCallId =
                msg.toolCallId !== undefined &&
                t.toolCallId === msg.toolCallId;
              const matchesName =
                msg.toolCallId === undefined && t.tool === msg.tool;
              const isRunning = t.status === "running";
              if (isRunning && (matchesCallId || matchesName)) {
                return {
                  ...t,
                  status: nextStatus as "done" | "error",
                  ...(msg.result ? { result: msg.result } : {}),
                  ...(msg.meta ? { meta: msg.meta } : {}),
                };
              }
              return t;
            });
            return { ...m, toolUses };
          }),
        );
        return;
      }

      if (msg.type === "heartbeat") {
        // If any run has empty content so far, show a "working..." hint on
        // its bubble. Picks the first empty run (usually there's only one).
        for (const run of runsRef.current.values()) {
          if (!run.streamContent) {
            const messageId = run.messageId;
            setMessages((prev) =>
              prev.map((m) =>
                m.id === messageId && !m.content
                  ? { ...m, content: "Agent is working..." }
                  : m,
              ),
            );
            break;
          }
        }
      }
    });
  }, [onChatMessage]);

  // ---- Approval handling moved out of useAgentChat ----
  //
  // Exec approvals now live in a global queue (see useExecApprovals) and
  // render as a modal dialog at the chat-shell level. The previous design
  // glued each pending approval onto a tool-use bubble inside `messages`
  // state, which:
  //   1. Accumulated orphans — no expiresAtMs honoring meant the queue
  //      grew forever.
  //   2. Bound the card to whichever bubble had a "running exec" first
  //      when scanning backwards, so it landed on stale bubbles offscreen.
  //   3. Forced fallback to the `/approve <id>` slash-command UX from
  //      OpenClaw's CLI when the inline card never rendered.
  //
  // Mirrors OpenClaw's native `execApprovalQueue` pattern at
  // ~/Desktop/openclaw/ui/src/ui/app-gateway.ts (`enqueueApprovalRequest`)
  // and ~/Desktop/openclaw/ui/src/ui/views/exec-approval.ts.
  //
  // This hook no longer subscribes to exec.approval.* events.

  // ---- Send message ----

  // Hoisted so both sendMessage and the flushQueue effect (added in Task 5)
  // can call it. dispatchNow assumes the busy-gate has already been checked.
  const dispatchNow = useCallback(
    async (message: string): Promise<void> => {
      const agentId = agentIdRef.current;
      if (!agentId) return;

      const userMsgId = `user-${crypto.randomUUID()}`;
      const newClientRunId = crypto.randomUUID();
      clientRunIdRef.current = newClientRunId;

      setMessages((prev) => [
        ...prev,
        { id: userMsgId, role: "user", content: message },
      ]);
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
        const errMsgId = `assistant-${crypto.randomUUID()}`;
        setMessages((prev) => [
          ...prev,
          { id: errMsgId, role: "assistant", content: `Error: ${errorMessage}` },
        ]);
        setIsStreaming(false);
        clientRunIdRef.current = null; // failure path resets busy gate
      }
    },
    [sendChat],
  );

  // Drain queued messages one at a time on busy→idle transitions.
  // We track the previous value of clientRunIdRef in a ref so a simple
  // effect can detect the falling edge without polling.
  const prevClientRunIdRef = useRef<string | null>(null);
  // Deliberate no-deps effect: must run after every render so the ref-based
  // falling-edge detector catches the busy→idle transition. The work is
  // O(1) (two ref reads + comparison) when no transition occurred. Mirrors
  // the pattern at apps/frontend/src/hooks/useChatGPTOAuth.ts:167.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => {
    const prev = prevClientRunIdRef.current;
    const curr = clientRunIdRef.current;
    prevClientRunIdRef.current = curr;
    // Connection guard: if the WS is currently down, don't pop+dispatch —
    // sendChat would throw (see useGateway.tsx) and dispatchNow's catch
    // would reset clientRunIdRef, but the queued message would be lost.
    // Leave the queue intact; when the WS reconnects and the next user-turn
    // completes, this effect re-runs on the next falling edge.
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

    // Analytics: emit `chat_aborted` on user intent (stop click / RPC).
    // Compute elapsed from turn start; if we never got a turn start
    // (shouldn't happen when isStreaming is true, but belt-and-braces)
    // fall back to 0. Clear the turn refs so the terminal `done` from
    // the backend doesn't also emit `chat_completed` for this same turn.
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

    // Immediately update local state so the UI feels responsive
    setIsStreaming(false);
    finalizeAllActiveRuns();
    // Close the cancel-before-first-event window: if runsRef was empty at
    // this point (cancel fired before any event for the in-flight turn),
    // finalizeAllActiveRuns had no runIds to tombstone, so the next late
    // first-event would spawn a ghost bubble without this flag.
    pendingCancelRef.current = true;
    // Cancel terminates the current user-turn. Queue is NOT cleared —
    // upstream behavior is that abort only stops the active run; queued
    // follow-ups keep draining. Users can clearQueue() explicitly if
    // they want everything gone.
    clientRunIdRef.current = null;
  }, [isStreaming, sendReq, sessionName, finalizeAllActiveRuns]);

  // ---- Clear messages ----

  const clearMessages = useCallback(() => {
    setMessages([]);
    const key = agentIdRef.current ? `${agentIdRef.current}:${sessionName}` : null;
    if (key) {
      _messageCache.delete(key);
    }
    setError(null);
    setIsStreaming(false);
    finalizeAllActiveRuns();
    // Same reasoning as cancelMessage: cover the case where clear fires
    // before any event for the in-flight turn has arrived.
    pendingCancelRef.current = true;
  }, [sessionName, finalizeAllActiveRuns]);

  // ---- External interface ----

  const externalMessages: AgentMessage[] = useMemo(
    () =>
      messages.map(({ role, content, thinking, toolUses, errorCode }) => ({
        role,
        content,
        ...(thinking ? { thinking } : {}),
        ...(toolUses?.length ? { toolUses } : {}),
        ...(errorCode ? { errorCode } : {}),
      })),
    [messages],
  );

  const needsBootstrap = !!(
    cacheKey &&
    _needsBootstrap.has(cacheKey) &&
    messages.length === 0 &&
    historyLoadState === "done"
  );

  return {
    messages: externalMessages,
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
