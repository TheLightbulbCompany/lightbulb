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
import { useSWRConfig } from "swr";
import {
  useGateway,
  type ChatIncomingMessage,
} from "@/hooks/useGateway";
import { useExecApprovalsContextOptional } from "@/hooks/useExecApprovals";
import { useActiveSession } from "@/hooks/useActiveSession";
import { capture } from "@/lib/analytics";
import { emitStep } from "@/components/getstarted/getStartedEvents";
import { notifyApiError } from "@/lib/notify";
import type {
  HistoryMessage,
  ContentBlock,
  ToolStreamEntry,
  ChatRenderItem,
  RenderGroup,
} from "./internal/types";
import { accumulateStreamText } from "@isol8/shared";
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
  // Quiet defensive fallback. The backend serialize gate now returns a benign
  // `agent_busy` frame (handled in the error branch below) instead of letting a
  // raw "session file locked" surface to the web client, so this should never
  // fire. It stays only to keep the bare technical lock string from reaching a
  // user on some untested edge — softened to plain "busy" copy, never an error
  // card (a lock error carries no errorKind, so it renders as plain text, not a
  // typed ChatErrorCard).
  [/session file locked/i, "Your agent is busy with another request. Please wait a moment and try again."],
  [/Model access is denied/i, "This model is not available. Try switching to a different model in settings."],
];

function friendlyError(raw: string): string {
  for (const [pattern, friendly] of ERROR_PATTERNS) {
    if (pattern.test(raw)) return friendly;
  }
  return raw;
}

// A user-initiated Stop is delivered as an `error` frame — connection_pool.py
// transforms chat.aborted into {"type":"error","message":"Agent run was
// cancelled"} (and websocket_chat.py synthesizes the identical frame when the
// abort RPC can't be confirmed). It carries no errorCode, so string-matching is
// the available signal, same as ERROR_PATTERNS above. An abort is the outcome
// the user asked for: it renders as a calm notice, not an error.
const ABORTED_PATTERN = /Agent run was cancelled/i;

// Builds the OpenClaw session key the read path (chat.history) queries. The
// write path constructs the authoritative key server-side; this must mirror
// it: 4-part `agent:{agentId}:{userId}:{sessionId}` for a real session, or the
// legacy 3-part `agent:{agentId}:{userId}` "General" session when sessionId is
// null so no history is orphaned.
function buildChatSessionKey(
  agentId: string,
  sessionName: string,
  sessionId: string | null,
): string {
  return sessionId
    ? `agent:${agentId}:${sessionName}:${sessionId}`
    : `agent:${agentId}:${sessionName}`;
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

/**
 * An ephemeral `/btw` (detached side-channel) answer. Held in transient state
 * only — NEVER pushed into `_messageCache` or `chatMessages`, so it vanishes on
 * refresh, mirroring OpenClaw's "Not saved to chat history" side card. `id` is
 * the side run's runId (deduped) when present, else a minted fallback.
 */
interface BtwSideResult {
  id: string;
  question: string;
  text: string;
  isError?: boolean;
}

export interface UseAgentChatReturn {
  /** Final rendered groups, ready for MessageList to walk. */
  renderGroups: RenderGroup[];
  /** Raw flat items before grouping — exposed for snapshot testing and StreamingStatusBar. */
  renderItems: ChatRenderItem[];
  /** Whether a user-turn is in flight (chatStream non-null OR live tools running OR clientRunIdRef set). */
  isStreaming: boolean;
  /** isStreaming (local anchor) OR containerRunActive (server truth via `run_active`). */
  busy: boolean;
  sendMessage: (message: string) => Promise<void>;
  cancelMessage: () => Promise<void>;
  clearMessages: () => void;
  isConnected: boolean;
  isLoadingHistory: boolean;
  needsBootstrap: boolean;
  /** The displayed history was trimmed to fit the transport frame limit. */
  historyTruncated: boolean;
  queuedMessages: QueuedMessage[];
  removeQueuedMessage: (id: string) => void;
  clearQueue: () => void;
  steerMessage: (id: string) => void;
  retryQueuedMessage: (id: string) => void;
  /** The active session id for this agent; null == the legacy General session. */
  activeSessionId: string | null;
  /** Mint + switch to a fresh session. Returns the new session id. */
  newSession: () => string;
  /** Switch the active session (null selects General). */
  selectSession: (sessionId: string | null) => void;
}

// =============================================================================
// Module-level message cache
//
// Survives component unmounts (navigation away from /chat and back) but
// clears on full page refresh. Keyed by `agentId:userId:sessionId` so each
// session's history is preserved independently.
// =============================================================================

const _messageCache = new Map<string, HistoryMessage[]>();
const _needsBootstrap = new Set<string>();

// Bounded backoff (ms) for re-reading chat.history when the FIRST read comes
// back empty AND there is no cached transcript to fall back on. A scaled-to-
// zero (trial) container still waking, a container mid-restart/update, or a
// session store that has not finished loading all return an empty-but-200 read
// that is NOT authoritatively empty — so a post-refresh fetch that races the
// container wake would otherwise blank the screen and offer the "start a
// conversation" bootstrap for a conversation that actually exists. Retrying a
// few times lets the store come up before we conclude the session is empty.
// One entry per retry; the array length caps the number of attempts.
const HISTORY_EMPTY_RETRY_DELAYS_MS = [500, 1250, 3000];
// Sessions whose last chat.history response was trimmed by the backend to fit
// the WS frame limit (payload carried `truncated: true`) — the transcript
// starts mid-conversation and the UI says so.
const _truncatedHistory = new Set<string>();

/**
 * The persisted transcript, bound to the active session's cache key.
 *
 * One useAgentChat instance serves ALL of an agent's sessions (a session
 * switch does not remount — only an agent switch does, via the key prop on
 * AgentChatWindow), so when `cacheKey` changes the previous session's
 * messages are still sitting in state. They are swapped for the new key's
 * cached transcript DURING render — React's "adjust state when a prop
 * changes" pattern — not in an effect, so that every effect always observes
 * a consistent (cacheKey, messages) pair. The cache write-back below relies
 * on this: were the swap effect-based, the write-back would fire once with
 * the new key and the old session's messages, poisoning the new session's
 * cache entry — which then short-circuits its history fetch and renders the
 * previous session's transcript.
 */
function useSessionScopedMessages(
  cacheKey: string | null,
): [HistoryMessage[], React.Dispatch<React.SetStateAction<HistoryMessage[]>>] {
  const readCache = (): HistoryMessage[] =>
    cacheKey ? _messageCache.get(cacheKey) ?? [] : [];

  const [messages, setMessages] = useState<HistoryMessage[]>(readCache);
  const [prevCacheKey, setPrevCacheKey] = useState(cacheKey);
  if (prevCacheKey !== cacheKey) {
    setPrevCacheKey(cacheKey);
    setMessages(readCache());
  }

  // Persist so switching away and back (or leaving /chat and returning)
  // restores this session instantly without a refetch.
  useEffect(() => {
    if (cacheKey && messages.length > 0) {
      _messageCache.set(cacheKey, messages);
    }
  }, [cacheKey, messages]);

  return [messages, setMessages];
}

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
        {
          type: "tool_use",
          id: e.toolCallId,
          name: e.name,
          args: e.args,
          // Carry the streamed incremental output into history (mirrors the
          // live sync path's spread). MessageList falls back to this when a
          // completed tool has no terminal result, matching upstream
          // app-tool-stream's single entry.output field — so a tool that
          // streamed output but ended without a real result keeps it in the
          // final folded bubble instead of only during execution.
          ...(e.progressText ? { progressText: e.progressText } : {}),
        },
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
// addCappedRunId
//
// Records a runId in a bounded Set, capping it to the last RUNID_SET_CAP entries
// (FIFO eviction via insertion-ordered Set) so it never grows unbounded over a
// long-lived chat. Used for both the drained-runIds set (an id only needs to
// outlive the done↔run_ended pair for its own run) and the side-run set (a
// /btw id only needs to outlive that side run's own event stream); in both
// cases, once a handful of newer runs have completed an older entry can never
// legitimately re-fire.
// =============================================================================

const RUNID_SET_CAP = 64;

function addCappedRunId(set: Set<string>, runId: string): void {
  set.add(runId);
  while (set.size > RUNID_SET_CAP) {
    const oldest = set.values().next().value;
    if (oldest === undefined) break;
    set.delete(oldest);
  }
}

// =============================================================================
// renderPartial
//
// Coerce an OpenClaw partialResult (string OR an array of {type,text} blocks)
// to a display string. Mirrors upstream formatToolOutput's text extraction so
// the live tool pill renders the same incremental output the control UI shows.
// =============================================================================

function renderPartial(partial: unknown): string {
  if (partial == null) return "";
  if (typeof partial === "string") return partial;
  if (Array.isArray(partial)) {
    return partial
      .map((b) =>
        b && typeof b === "object" && "text" in b ? String((b as { text?: unknown }).text ?? "") : "",
      )
      .join("");
  }
  return "";
}

// =============================================================================
// Hook
//
// NOTE: Only one useAgentChat instance should be active at a time. The backend
// protocol does not tag chunk/done/error messages with an agent_id, so
// concurrent instances would receive each other's messages. The UI enforces
// this by rendering a single AgentChatWindow.
// =============================================================================

export function useAgentChat(
  agentId: string | null,
  sessionName: string,
  // Fired once per successfully-completed turn (mirrors the internal
  // `chat_completed` capture below). Deliberately generic/billing-agnostic —
  // this hook has no billing context — so callers that DO have it (e.g.
  // AgentChatWindow, which already reads useBilling) can layer trial-specific
  // milestones (trial_first_agent_response) on top without this hook knowing
  // what a "trial" is.
  onChatCompleted?: () => void,
): UseAgentChatReturn {
  const { isConnected, sendChat, send, onChatMessage, sendReq } = useGateway();

  // Shared SWR mutate, used to force ONE revalidation of the
  // `/container/status` key that useProvisioningState owns when a chat send
  // lands on a scaled-to-zero (trial) container — see the `container_waking`
  // branch in the onChatMessage handler below. We reach it via useSWRConfig
  // rather than importing useProvisioningState().refresh() so this generic,
  // billing-agnostic hook stays free of Clerk's useAuth (which every consumer
  // and unit test would otherwise have to provide); mutate hits the exact key
  // the sibling ProvisioningStepper subscribes to, so its isRegressingFromReady
  // machinery flips to the cold-start UI as soon as the fresh (non-ready) row
  // arrives. Mirrored to a ref so the once-registered handler reads it without
  // a resubscribe.
  const { mutate: swrMutate } = useSWRConfig();
  const swrMutateRef = useRef(swrMutate);
  useEffect(() => {
    swrMutateRef.current = swrMutate;
  }, [swrMutate]);

  const { activeSessionId, newSession, selectSession } =
    useActiveSession(agentId);
  const activeSessionIdRef = useRef(activeSessionId);
  useEffect(() => {
    activeSessionIdRef.current = activeSessionId;
  }, [activeSessionId]);

  // Cache key includes session name AND active session id so distinct sessions
  // (and org members) keep independent history caches.
  const cacheKey = agentId
    ? `${agentId}:${sessionName}:${activeSessionId ?? ""}`
    : null;

  // Persisted messages — from chat.history + chat.final synthesis. Bound to
  // the active session's cache key: a session switch swaps in that session's
  // cached transcript (see useSessionScopedMessages).
  const [chatMessages, setChatMessages] = useSessionScopedMessages(cacheKey);

  // Live state for the in-flight run. Refs mirror the state so the
  // onChatMessage handler (registered once per mount with stable deps)
  // can read the live values without stale-closure bugs.
  const [chatStream, _setChatStreamRaw] = useState<string | null>(null);
  const chatStreamRef = useRef<string | null>(null);
  const chatStreamRunIdRef = useRef<string | null>(null);
  const chatStreamStartedAtRef = useRef<number | null>(null);
  // Committed-prefix guard: streamCumulativeRef reconstructs the run's FULL
  // assistant text from the wire frames (append deltas or full-text resyncs —
  // see accumulateStreamText). When a tool interrupts streaming we commit the
  // in-progress chatStream into chatStreamSegments; the reconstruction keeps
  // accumulating (pre-tool + post-tool text), so we must slice off the
  // already-committed prefix before storing the new chatStream. Otherwise the
  // segment + tail would both contain the pre-tool text and the synthesized
  // assistant message at `done` would duplicate it.
  const streamCommittedOffsetRef = useRef<number>(0);
  // Accumulated full text of the live run's assistant / thinking streams.
  // Reset at run boundaries and every live-state clear; a full-text
  // (non-append) frame replaces the buffer outright, which is how a client
  // that missed deltas self-corrects.
  const streamCumulativeRef = useRef<string | null>(null);
  const thinkingCumulativeRef = useRef<string | null>(null);
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

  // ---- Stream paint throttle ----
  //
  // Chunk/thinking frames arrive every ~150ms (OpenClaw's gateway coalescer),
  // but painting cost grows with message length (the live bubble re-parses
  // its whole markdown). Mirror the tool-stream throttle: the refs above are
  // updated synchronously per frame (fold/segment logic reads refs), while
  // the React paint is coalesced at 80ms. The flush paints FROM the refs, so
  // a timer that fires after a clear/commit converges to the cleared value —
  // it can never resurrect stale text.
  const STREAM_PAINT_THROTTLE_MS = 80;
  const streamPaintTimerRef = useRef<number | null>(null);
  const paintStreamNow = useCallback(() => {
    if (streamPaintTimerRef.current !== null) {
      window.clearTimeout(streamPaintTimerRef.current);
      streamPaintTimerRef.current = null;
    }
    _setChatStreamRaw(chatStreamRef.current);
    _setChatStreamThinkingRaw(chatStreamThinkingRef.current);
  }, []);
  const scheduleStreamPaint = useCallback(() => {
    if (streamPaintTimerRef.current !== null) return;
    streamPaintTimerRef.current = window.setTimeout(() => {
      streamPaintTimerRef.current = null;
      _setChatStreamRaw(chatStreamRef.current);
      _setChatStreamThinkingRaw(chatStreamThinkingRef.current);
    }, STREAM_PAINT_THROTTLE_MS);
  }, []);
  useEffect(() => {
    return () => {
      if (streamPaintTimerRef.current !== null) {
        window.clearTimeout(streamPaintTimerRef.current);
        streamPaintTimerRef.current = null;
      }
    };
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

  // Commit the in-progress stream tail as a segment before a pill is appended.
  // Any handler that pushes onto toolStreamOrderRef (a real tool_start OR a
  // live activity pill) must call this FIRST so foldLiveTurnIntoContent pairs
  // the just-streamed text with the new pill in chronological order. Always
  // pushes a segment (possibly empty-text) so chatStreamSegments[i] stays
  // index-aligned with the i-th pill — buildChatItems/fold rely on the
  // contract that segments[i] is "the text immediately before pill[i]", and
  // they skip empty segments so the sentinel is invisible at render time.
  // Without this, the streamed text stays in tailStream (folded LAST) while
  // the pill folds first, so the pill renders BEFORE the earlier text. The
  // committed length advances streamCommittedOffsetRef so the next post-pill
  // chunk (cumulative from start-of-run) slices off the already-committed
  // prefix. Does NOT clear chatStreamRunIdRef — the same run continues.
  const commitStreamBeforePill = useCallback(() => {
    const priorStream = chatStreamRef.current ?? "";
    setChatStreamSegments((segs) => [...segs, { text: priorStream, ts: Date.now() }]);
    if (priorStream.length > 0) {
      streamCommittedOffsetRef.current += priorStream.length;
      setChatStream(null);
    }
  }, [setChatStreamSegments, setChatStream]);

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

  // Server-truth busy signal, independent of this tab's local isStreaming
  // anchor. Set/cleared by the `run_active` frame (backend forwards
  // OpenClaw's sessions.changed for the initiating member) and belt-and-
  // suspenders cleared on the authoritative terminal frames + on a
  // connection drop, so a lost `run_active:false` can't strand it true.
  const [containerRunActive, setContainerRunActive] = useState(false);

  // Ephemeral `/btw` side-result cards. Transient by design: never written to
  // _messageCache or chatMessages, so they don't survive a refresh and are not
  // part of the persisted transcript — matching OpenClaw's "Not saved to chat
  // history" side card. Reset when the agent/session (cacheKey) changes so a
  // previous session's asides don't bleed into another.
  const [btwResults, setBtwResults] = useState<BtwSideResult[]>([]);
  useEffect(() => {
    setBtwResults([]);
  }, [cacheKey]);

  const [historyLoadState, setHistoryLoadState] = useState<"idle" | "loading" | "done">("idle");
  const isLoadingHistory = historyLoadState === "loading";
  // Bumped to force the history-fetch effect below to re-run after a
  // server-side session mutation (/reset, /compact, /new) — see
  // invalidateHistoryAndRefetch.
  const [historyRefetchNonce, setHistoryRefetchNonce] = useState(0);

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

  // Side-channel /btw run ids. A /btw (or /side) aside runs CONCURRENTLY with
  // the primary turn on the same sessionKey + agent_id, but as a separate run
  // with its OWN runId. We mint that runId on send (passed as clientRunId so it
  // becomes the backend idempotencyKey → the runId every side event carries)
  // and record it here so the event handler can keep the side run's
  // stream/terminal events OFF the primary turn's live state. Mirrors upstream:
  // sendDetachedChatMessage generates the runId (controllers/chat.ts) and the
  // client tracks it (TUI noteLocalBtwRunId / isLocalBtwRunId). Bounded set.
  const sideRunIdsRef = useRef<Set<string>>(new Set());

  // agentId mirror for callback access without stale closures.
  const agentIdRef = useRef(agentId);

  // onChatCompleted mirror — the onChatMessage subscription effect below
  // depends only on `[onChatMessage]` (a stable identity), so it captures
  // its closure once at mount; a ref keeps the callback current across
  // re-renders without forcing a resubscribe on every identity change of an
  // inline caller-supplied function.
  const onChatCompletedRef = useRef(onChatCompleted);
  useEffect(() => {
    onChatCompletedRef.current = onChatCompleted;
  }, [onChatCompleted]);

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
  //
  // Capped to the last N runIds (insertion-ordered Set, FIFO eviction) so it
  // never grows unbounded over a long-lived chat. A drained runId only needs
  // to outlive the done↔run_ended pair for its own run; once a handful of
  // newer runs have completed, an older entry can never legitimately re-fire.
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
        ...(activeSessionIdRef.current ? { session_id: activeSessionIdRef.current } : {}),
      });
      setQueuedMessages((prev) => prev.filter((m) => m.id !== id));
    },
    [isConnected, send],
  );

  useEffect(() => {
    agentIdRef.current = agentId;
  }, [agentId]);

  // ---- Tool stream sync (Map → state array) ----
  //
  // Sync toolStreamById Map → chatToolMessages array (state) for rendering.
  // Throttled at 80ms (matches upstream) but force-flushed on tool_end.
  const toolStreamSyncTimerRef = useRef<number | null>(null);
  const TOOL_STREAM_THROTTLE_MS = 80;

  const syncToolStreamMessages = useCallback((force = false) => {
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
              // Live incremental output (tool phase:update / command_output),
              // shown in the running pill until the terminal tool_result lands.
              ...(e.progressText ? { progressText: e.progressText } : {}),
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
    // Force-flush immediately only when the CALLER signals a terminal event
    // (tool_end/tool_error, or an accepted activity pill) — mirrors upstream's
    // scheduleToolStreamSync(host, phase === "result"). A pending throttle
    // timer is cleared so the terminal tool_result lands at once instead of
    // ~80ms late (which would flash a half-finished tool card). Deriving this
    // from map state ("any entry terminal") instead is wrong: once any tool/
    // activity has completed, every high-frequency command_output/tool_progress
    // frame would bypass the throttle and trigger a React render per chunk.
    if (force) {
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
  // Pending empty-history retry timer (see the history-fetch effect). Held in a
  // ref so the effect cleanup can cancel it on unmount / session switch.
  const historyRetryTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // "Is this hook's history-fetch effect currently mounted?" Re-affirmed true at
  // the top of every effect run (BEFORE the latch early-return) and set false
  // only in the effect cleanup. Unlike a per-effect-instance boolean, this
  // survives React StrictMode's dev double-invoke (mount → cleanup → re-mount):
  // the phantom cleanup sets it false, but the real re-mount sets it true again
  // — so a first fetch still owned by the phantom instance's closure keeps
  // retrying an empty read instead of collapsing to an immediate bootstrap. A
  // GENUINE unmount leaves it false (no re-mount follows), stopping the retry.
  const historyEffectMountedRef = useRef(false);

  // Monotonic counter of LOCAL transcript mutations that must survive the
  // mount-time history revalidation: optimistic user appends, folded run
  // output, error bubbles, user-initiated clears. The revalidation fetch
  // below snapshots it at issue time; a different value at resolve time means
  // the server snapshot predates a local mutation and applying it would drop
  // that mutation. Deliberately per-instance (not per-session): an unrelated
  // session's mutation can suppress another session's apply, but that
  // over-suppression is safe — we keep local state, which self-heals on the
  // next mount's revalidation — whereas under-suppression loses messages.
  const transcriptMutationSeqRef = useRef(0);

  useEffect(() => {
    if (!agentId || !cacheKey || !isConnected) return;
    // Affirm mounted BEFORE the latch early-return: under StrictMode the phantom
    // re-mount early-returns here (cacheKey already latched by the first mount),
    // so this must run first to re-mark the hook mounted and keep the first
    // fetch's retry loop alive.
    historyEffectMountedRef.current = true;
    if (historyLoadedRef.current.has(cacheKey)) return;
    // Cached transcript paints instantly, but a run that completed while
    // this hook was unmounted only exists server-side — so always fetch
    // once per mount and let the server result replace the cache.
    const hasCache = _messageCache.has(cacheKey);
    const sessionKey = buildChatSessionKey(agentId, sessionName, activeSessionId);
    if (!hasCache) {
      Promise.resolve().then(() => setHistoryLoadState("loading"));
    }
    const mutationSeqAtIssue = transcriptMutationSeqRef.current;
    // Latch BEFORE the async fetch, not inside `.then`: two effect runs before
    // the first resolves (an isConnected flicker, a StrictMode double-mount)
    // would each pass the has()-guard above and fire a duplicate chat.history.
    // The `.catch` un-latches so a failed fetch still retries on the next run.
    historyLoadedRef.current.add(cacheKey);

    // Stable Set (the ref is never reassigned); captured for the cleanup so it
    // doesn't read ref.current there (react-hooks/exhaustive-deps).
    const loadedKeys = historyLoadedRef.current;

    const runFetch = (attempt: number): void => {
      sendReq("chat.history", { sessionKey, limit: 200 })
        .then((result: unknown) => {
          const historyResult = result as { messages?: HistoryMessage[]; truncated?: boolean };
          // A local transcript mutation while this fetch was in flight (a sent
          // message, a folded run, a clear) means the snapshot is stale —
          // applying it would silently drop that mutation from BOTH the live
          // view and the cache. On cached mounts this fetch is invisible (no
          // loading state, input enabled), so a send racing it is routine.
          // Keep local state; the next mount's revalidation self-heals any
          // divergence. The truncated flag follows the same skip: it describes
          // the discarded snapshot, and flipping it while keeping the local
          // transcript would show a banner that doesn't match the screen.
          const staleSnapshot = transcriptMutationSeqRef.current !== mutationSeqAtIssue;
          if (!staleSnapshot) {
            if (historyResult?.truncated === true) {
              _truncatedHistory.add(cacheKey);
            } else {
              _truncatedHistory.delete(cacheKey);
            }
          }
          const raw = Array.isArray(historyResult?.messages) ? historyResult.messages : [];
          const projected = projectChatHistoryMessages(raw);
          if (projected.length === 0) {
            // Server has nothing: only decide bootstrap when we ALSO have no
            // cache — an existing cached transcript must not regress to the
            // bootstrap suggestion because of an empty/partial server read.
            if (!hasCache) {
              // An empty-but-200 read is NOT a reliable "authoritatively empty"
              // signal (container mid-restart/update, session store not yet
              // loaded, session-key casing mismatch, or a trial container still
              // waking from scale-to-zero all return it). While we're still on
              // this session and have retries left, re-read after a short
              // backoff (the store loads shortly after the WS connects) and stay
              // on the loading skeleton — never flash the bootstrap suggestion
              // for a conversation that exists (it "comes back" once the
              // container is up). Only after the retries exhaust — or if the
              // user already switched away — do we conclude it is empty. (The
              // cleanest long-term fix is a backend authoritative-empty marker
              // on chat.history; until then this bounded retry is the frontend
              // guard.)
              const stillCurrent =
                historyEffectMountedRef.current &&
                agentIdRef.current === agentId &&
                activeSessionIdRef.current === (activeSessionId ?? null);
              if (stillCurrent && attempt < HISTORY_EMPTY_RETRY_DELAYS_MS.length) {
                historyRetryTimerRef.current = setTimeout(() => {
                  historyRetryTimerRef.current = null;
                  runFetch(attempt + 1);
                }, HISTORY_EMPTY_RETRY_DELAYS_MS[attempt]);
                return; // keep isLoadingHistory true (skeleton); decide later
              }
              _needsBootstrap.add(cacheKey);
            }
            setHistoryLoadState("done");
            return;
          }
          setHistoryLoadState("done");
          if (staleSnapshot) return;
          if (agentIdRef.current !== agentId) return;
          // Always cache under the key this fetch was issued for; only touch
          // the live transcript if the user is still ON that session. A fetch
          // that resolves after a session switch must not stomp the new view.
          _messageCache.set(cacheKey, projected);
          if (activeSessionIdRef.current === (activeSessionId ?? null)) {
            setChatMessages(projected);
          }
        })
        .catch((err: unknown) => {
          console.warn("Failed to fetch chat history:", err);
          // Un-latch (the marker was set BEFORE the fetch for in-flight dedupe):
          // a failed fetch (gateway drop, container restart) must retry on the
          // next visit or reconnect instead of leaving this session blank until
          // page reload.
          historyLoadedRef.current.delete(cacheKey);
          setHistoryLoadState("done");
        });
    };

    runFetch(0);

    return () => {
      // Mark this hook's history effect unmounted. A StrictMode phantom cleanup
      // sets this false but the immediately-following real re-mount sets it true
      // again (see the top-of-effect affirm), so the in-flight retry loop only
      // stops on a GENUINE unmount. Navigating to a different agent/session is
      // caught separately by the agentIdRef/activeSessionIdRef checks in the
      // retry decision — this flag is only the "hook is gone" guard.
      //
      // Also cancel a pending empty-read retry so it can't fire for a session
      // the user switched away from. If a retry WAS pending — i.e. we're between
      // retries — un-latch so a later effect run (reconnect, revisit) re-fetches
      // instead of early-returning on the latch and stranding the session on the
      // loading skeleton. A first fetch still in flight (no timer pending) keeps
      // its latch, preserving in-flight dedupe.
      historyEffectMountedRef.current = false;
      if (historyRetryTimerRef.current !== null) {
        clearTimeout(historyRetryTimerRef.current);
        historyRetryTimerRef.current = null;
        loadedKeys.delete(cacheKey);
      }
    };
  }, [agentId, cacheKey, sessionName, activeSessionId, isConnected, sendReq, setChatMessages, historyRefetchNonce]);

  // ---- Reset live-run state when the active session switches ----
  // Agent switches are handled upstream (AgentChatWindow's prevAgentIdRef);
  // this covers switching sessions WITHIN one agent, which shares the same
  // useAgentChat instance. The persisted transcript (chatMessages) is swapped
  // during render by useSessionScopedMessages; this effect only clears the
  // in-flight run's stream/tool/error state, which is not cache-keyed.
  const prevSessionRef = useRef<string | null>(activeSessionId);
  useEffect(() => {
    if (prevSessionRef.current === activeSessionId) return;
    prevSessionRef.current = activeSessionId;
    setChatStream(null);
    chatStreamRunIdRef.current = null;
    streamCommittedOffsetRef.current = 0;
    streamCumulativeRef.current = null;
    setChatStreamThinking(null);
    thinkingCumulativeRef.current = null;
    chatStreamThinkingRunIdRef.current = null;
    setChatStreamSegments([]);
    setChatToolMessages([]);
    toolStreamByIdRef.current.clear();
    toolStreamOrderRef.current = [];
    setBtwResults([]);
    setIsStreaming(false);
    // Scope the server-truth busy bit to the chat it came from: the old
    // session's later run_active:false/terminal is tagged with the old
    // session_id and gets dropped by the demux, so without this the spinner
    // would strand "on" for the newly-selected session.
    setContainerRunActive(false);
    clientRunIdRef.current = null;
    resumeExpectedRef.current = false;
  }, [
    activeSessionId,
    setChatStream,
    setChatStreamThinking,
    setChatStreamSegments,
    setChatToolMessages,
    setIsStreaming,
  ]);

  // ---- Invalidate + refetch history ----
  //
  // Used after a server-side session mutation (/reset, /compact, /new) so the
  // displayed transcript can't diverge from the now-reset session. Drops the
  // module-cache entry and the loaded-marker for this session, clears the live
  // chatMessages state (covers the empty-history case, where the fetch leaves
  // chatMessages untouched), then bumps a nonce so the history-fetch effect
  // above re-runs its EXISTING fetch path — no duplicate fetch logic here.
  const invalidateHistoryAndRefetch = useCallback(() => {
    const key = agentIdRef.current
      ? `${agentIdRef.current}:${sessionName}:${activeSessionIdRef.current ?? ""}`
      : null;
    if (!key) return;
    _messageCache.delete(key);
    historyLoadedRef.current.delete(key);
    setChatMessages([]);
    setHistoryRefetchNonce((n) => n + 1);
  }, [sessionName, setChatMessages]);

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

      // Session demux. Once one agent has many sessions (or background tabs),
      // the owner-keyed fan-out delivers frames for OTHER sessions here. A
      // frame is ours only when its stamped session_id matches the active
      // session. Compare NORMALIZED (a missing/undefined id == null == the
      // legacy "General" session) so an UNTAGGED General frame is DROPPED while
      // a named session is active. A bare `"session_id" in msg` guard would
      // fail-open on that case and let a General run's chunks leak into the
      // named-session view. Safe because the backend tags EVERY 4-part webchat
      // frame and NEVER tags 3-part General frames. Broadcasts
      // (heartbeat/update_available) carry no session_id and untagged errors
      // were already allowed through above.
      const frameSessionId = "session_id" in msg ? (msg.session_id ?? null) : null;
      if (
        !isUntaggedBroadcast &&
        !isUntaggedError &&
        frameSessionId !== activeSessionIdRef.current
      ) {
        // A terminal frame for ANOTHER session of this agent means that
        // session's server-side transcript changed while it wasn't displayed.
        // Never touch the live view — but drop the background session's
        // history cache so switching back refetches instead of rendering the
        // stale pre-run messages (the reply would otherwise be invisible
        // until a full page reload).
        if (msg.type === "done" || msg.type === "error" || msg.type === "run_ended") {
          const staleKey = agentIdRef.current
            ? `${agentIdRef.current}:${sessionName}:${frameSessionId ?? ""}`
            : null;
          if (staleKey) {
            _messageCache.delete(staleKey);
            historyLoadedRef.current.delete(staleKey);
          }
        }
        return;
      }

      // `/btw` side-channel ANSWER. OpenClaw renders these on a distinct channel
      // it surfaces as a side card "Not saved to chat history"; the backend maps
      // chat.side_result → this `btw_result` frame. Hold it in transient
      // btwResults state (never folded into chatMessages / _messageCache) and
      // render it as an ephemeral side card. This MUST run BEFORE the side-run
      // segregation below: a btw_result carries the side run's runId (tracked in
      // sideRunIdsRef), so the segregation's blanket drop would otherwise swallow
      // it. It never touches the primary turn's live state or busy gate — it is
      // purely additive. Dedupe by runId so a re-delivered frame can't double-add.
      if (msg.type === "btw_result") {
        const id = msg.runId ?? `btw-${crypto.randomUUID()}`;
        setBtwResults((prev) =>
          prev.some((b) => b.id === id)
            ? prev
            : [
                ...prev,
                {
                  id,
                  question: msg.question,
                  text: msg.text,
                  ...(msg.isError ? { isError: true } : {}),
                },
              ],
        );
        return;
      }

      // Segregate side-channel /btw runs. A /btw aside runs concurrently with
      // the primary turn but as a SEPARATE run carrying its own (frontend-
      // minted) runId. Its stream/terminal events MUST NOT touch the primary
      // turn's live state — otherwise a side chunk would hijack the live
      // stream and a side `done`/`error` would fold+clear the still-running
      // primary turn. Upstream solves this by gating apply on the active run
      // (controllers/chat.ts handleChatEvent activeRunMatches) and routing btw
      // output to a separate side-result card; Isol8 has no side-result surface
      // yet, so we drop side events here (the primary run is unaffected). This
      // MUST run before the resume-adoption transfer below so a side runId is
      // never mistaken for the primary run's post-approval continuation.
      // Clean up the id ONLY on `run_ended` — the side run's terminal sequence
      // is the done/error frame FOLLOWED BY a synthesized run_ended (backend
      // forwards both, run_ended last). Deleting on the first terminal (done)
      // would let the trailing run_ended fall through to the normal path, where
      // — with no primary run active — it could be adopted and drain the queue
      // even though only a detached side note ended. run_ended is the last
      // event for the run, so keeping the id until then drops the whole
      // sequence. (If run_ended is somehow lost, the bounded FIFO set evicts
      // the id eventually.) "runId" in msg narrows away heartbeat/update_available.
      if (
        "runId" in msg &&
        typeof msg.runId === "string" &&
        sideRunIdsRef.current.has(msg.runId)
      ) {
        if (msg.type === "run_ended") {
          sideRunIdsRef.current.delete(msg.runId);
        }
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

      // Same tombstone protection as the cancelled-run guard above, extended to
      // the sub-stream types. Must run BEFORE run-adoption: otherwise a late
      // sub-stream for a cleared run re-adopts it (sets clientRunIdRef) and
      // re-arms the busy gate before it can be dropped, queuing the user's next
      // send behind a dead run.
      if (
        (msg.type === "tool_progress" ||
          msg.type === "command_output" ||
          msg.type === "agent_activity") &&
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
        msg.type !== "run_active" &&
        msg.runId &&
        msg.runId !== clientRunIdRef.current
      ) {
        clientRunIdRef.current = msg.runId;
        resumeExpectedRef.current = false;
      }

      // Adopt an observed run as the active run when we have none — mirrors
      // upstream handleChatEvent (controllers/chat.ts:711 `if (!chatRunId &&
      // sessionMatches) chatRunId = payload.runId`; tui-event-handlers.ts:461).
      // A tab OBSERVING another tab's run (owner-keyed fan-out delivers its
      // events here) thereby knows it's busy and ENQUEUES subsequent sends
      // rather than dispatching into the backend serialize gate. This is the
      // primary fix for the multi-tab case; the `agent_busy` rollback below is
      // only the safety net for the sub-second race where this tab submits
      // before the run's first event has reached it. Guards: skip side (`/btw`)
      // runs (they never own the primary window) and already-finalized runs
      // (drainedRunIds — a late/stray event must not re-arm the gate).
      if (
        clientRunIdRef.current === null &&
        "runId" in msg &&
        typeof msg.runId === "string" &&
        !sideRunIdsRef.current.has(msg.runId) &&
        !drainedRunIdsRef.current.has(msg.runId)
      ) {
        clientRunIdRef.current = msg.runId;
      }

      // Server-truth busy signal — see containerRunActive state comment.
      // Carries no runId (it isn't tied to a specific run), so none of the
      // runId-keyed guards above apply; just mirror the server's flag.
      if (msg.type === "run_active") {
        setContainerRunActive(!!msg.active);
        return;
      }

      if (msg.type === "chunk") {
        const runId = msg.runId;
        // Run boundary: either first event of a fresh turn (current === null)
        // or a new sub-run (current !== runId). Commit any in-progress stream
        // as a segment and reset the committed offset + cumulative buffer so
        // the new run's reconstruction starts from scratch.
        const isRunBoundary = chatStreamRunIdRef.current === null || chatStreamRunIdRef.current !== runId;
        if (isRunBoundary) {
          const prior = chatStreamRef.current;
          if (prior && prior.length > 0) {
            setChatStreamSegments((segs) => [...segs, { text: prior, ts: Date.now() }]);
          }
          streamCommittedOffsetRef.current = 0;
          streamCumulativeRef.current = null;
        }
        // Reconstruct the run's cumulative text (frames arrive as append
        // deltas or full-text resyncs), then slice off the already-committed
        // prefix. Refs update synchronously; the paint is throttled.
        const cumulative = accumulateStreamText(streamCumulativeRef.current, msg);
        streamCumulativeRef.current = cumulative;
        const tail = cumulative.slice(streamCommittedOffsetRef.current);
        chatStreamRef.current = tail.length > 0 ? tail : null;
        chatStreamRunIdRef.current = runId;
        if (chatStreamStartedAtRef.current === null) {
          chatStreamStartedAtRef.current = Date.now();
        }
        // Paint immediately at a run boundary (the prior tail just moved into
        // segments state — a throttled paint would briefly render it twice).
        if (isRunBoundary) {
          paintStreamNow();
        } else {
          scheduleStreamPaint();
        }
        if (!isStreamingRef.current) setIsStreaming(true);
        return;
      }

      if (msg.type === "thinking") {
        const runId = msg.runId;
        if (chatStreamThinkingRunIdRef.current !== null && chatStreamThinkingRunIdRef.current !== runId) {
          // New run's live thinking replaces the previous run's block (the
          // final assistant message carries finalized thinking if any) —
          // restart the reconstruction buffer.
          thinkingCumulativeRef.current = null;
        }
        const cumulative = accumulateStreamText(thinkingCumulativeRef.current, msg);
        thinkingCumulativeRef.current = cumulative;
        chatStreamThinkingRef.current = cumulative;
        chatStreamThinkingRunIdRef.current = runId;
        scheduleStreamPaint();
        if (!isStreamingRef.current) setIsStreaming(true);
        return;
      }

      if (msg.type === "tool_start") {
        const runId = msg.runId;
        const toolCallId = msg.toolCallId;
        if (!toolCallId) return;

        // Commit the in-progress stream tail before this pill so segments stay
        // index-aligned with the tool order (see commitStreamBeforePill).
        commitStreamBeforePill();

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
          // Terminal event → force-flush so the tool_result lands at once.
          syncToolStreamMessages(true);
          return;
        }
        // No live entry — likely the post-approval continuation for a
        // tool whose tool_use was already folded into a synthesized
        // HistoryMessage at the interim chat.final (live state was
        // cleared there). Append the tool_result block to that message
        // so the approved command's output reaches history and the UI,
        // instead of being silently dropped.
        //
        // Local mutation the in-flight history revalidation must not
        // clobber. Bumped unconditionally (the updater below may return
        // prev unchanged) — over-suppression is safe, see the seq ref.
        transcriptMutationSeqRef.current += 1;
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

      // --- Dropped sub-streams surfaced additively ---
      // These attach live tool progress, command output, and low-noise
      // agent-activity pills. They never touch the run-lifecycle
      // refs (clientRunIdRef / resumeExpectedRef / drainedRunIds) — those are
      // owned by the chunk/tool/done/run_ended handlers. Tombstoned-run frames
      // for these types are dropped up front, next to the cancelled-run guard,
      // so the drop precedes run-adoption.

      if (msg.type === "tool_progress") {
        // Replace the live pill's progress with the latest partialResult (it is
        // cumulative per the upstream tool stream). No live entry → nothing to
        // attach to (the tool already terminated), so drop it.
        const toolCallId = msg.toolCallId;
        if (!toolCallId) return;
        // An observed in-flight run (e.g. a tab that reconnected/opened mid-run
        // and whose first frame for this tool is a progress frame) means the
        // run is busy — mark it streaming so the stop affordance shows and
        // cancelMessage works (it guards on isStreaming). Do this even when
        // there's no local pill to update: run adoption above already claimed
        // this run, but only the stream-type branches flip the busy gate.
        // Mirrors tool_start (no run-match gate, same guarded call).
        if (!isStreamingRef.current) setIsStreaming(true);
        const entry = toolStreamByIdRef.current.get(toolCallId);
        if (!entry) return;
        // A status-only update (no partialResult) carries no new output. Skip it
        // instead of clearing the accumulated progress — renderPartial(null) is
        // "", which would wipe prior command_output. Our backend forwards
        // partialResult-less update frames; upstream's stream rarely emits them.
        const partial = renderPartial(msg.partialResult);
        if (!partial) return;
        entry.progressText = partial;
        entry.updatedAt = Date.now();
        toolStreamByIdRef.current.set(toolCallId, entry);
        syncToolStreamMessages();
        return;
      }

      if (msg.type === "command_output") {
        // Append streamed stdout/stderr to the live pill's progress. command_output
        // deltas are incremental (not cumulative), so concatenate.
        const toolCallId = msg.toolCallId;
        if (!toolCallId || !msg.output) return;
        // Same observed-run busy signal as tool_progress: a reconnecting tab's
        // first frame can be streamed stdout for a tool it has no live pill
        // for. Mark the adopted run streaming so the stop affordance shows and
        // cancelMessage works, even with no local pill. Mirrors tool_start.
        if (!isStreamingRef.current) setIsStreaming(true);
        const entry = toolStreamByIdRef.current.get(toolCallId);
        if (!entry) return;
        entry.progressText = (entry.progressText ?? "") + msg.output;
        entry.updatedAt = Date.now();
        toolStreamByIdRef.current.set(toolCallId, entry);
        syncToolStreamMessages();
        return;
      }

      if (msg.type === "agent_activity") {
        // Surface plan/patch/item/acp as a single low-noise pill. Keyed by a
        // synthetic id (activity+runId) so repeated activities of the same kind
        // within a run collapse into one updating pill rather than stacking.
        // Status "done" because activities are point-in-time, not long-running
        // (the sync call below force-flushes it as a terminal event).
        const synthId = `activity:${msg.activity}:${msg.runId}`;
        const now = Date.now();
        const existing = toolStreamByIdRef.current.get(synthId);
        const entry: ToolStreamEntry = existing
          ? { ...existing, name: msg.title || msg.activity, args: msg.detail, updatedAt: now }
          : {
              toolCallId: synthId,
              runId: msg.runId,
              name: msg.title || msg.activity,
              args: msg.detail,
              status: "done",
              kind: "activity",
              startedAt: now,
              updatedAt: now,
            };
        toolStreamByIdRef.current.set(synthId, entry);
        if (!toolStreamOrderRef.current.includes(synthId)) {
          // A NEW pill takes a new slot in the tool order, so commit the live
          // stream tail FIRST (exactly as tool_start does) — otherwise the
          // text streamed before this activity folds after the pill and renders
          // out of order. An UPDATE to an existing pill keeps its slot, so skip
          // the commit there (committing again would push a spurious segment).
          commitStreamBeforePill();
          toolStreamOrderRef.current.push(synthId);
        }
        // Activity pills are point-in-time (status "done") → terminal, so
        // force-flush like tool_end.
        syncToolStreamMessages(true);
        // An accepted activity pill means the run is busy — mark it streaming so
        // the stop affordance shows and cancelMessage works (it guards on
        // isStreaming). Mirrors the chunk/thinking/tool_start branches, which
        // flip streaming when they accept a run's first frame. Cancelled-run
        // frames are dropped by the tombstone guard above before adoption, so
        // a dead run never reaches here. Set once for the branch — covers both
        // the new-pill and activity-update paths (an update is still busy).
        if (!isStreamingRef.current) setIsStreaming(true);
        return;
      }

      if (msg.type === "run_ended") {
        // The AUTHORITATIVE server signal that this run is truly over. It
        // trails the terminal done/error for the same run and is the
        // canonical drain trigger — the queue drains only when the run is
        // truly over per the SERVER, never on a local terminal/error edge.
        // Belt-and-suspenders: also force-close containerRunActive here so a
        // lost/out-of-order `run_active:false` can't strand the spinner.
        setContainerRunActive(false);
        //
        // Dedupe by runId: the terminal done/error handler below already
        // drained this run (or, for the session-less edge, a `done` with no
        // run_ended did). If so, this is a no-op so we don't double-send.
        const runId = msg.runId;
        if (drainedRunIdsRef.current.has(runId)) {
          return;
        }
        addCappedRunId(drainedRunIdsRef.current, runId);
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
        // Belt-and-suspenders: `done` is a genuine terminal for the vast
        // majority of turns (the approval-pause interim chat.final is the
        // one exception, but closing containerRunActive here is still safe
        // — isStreaming stays armed via the approval gate below, and a
        // fresh `run_active:true` re-opens it if the container is still
        // genuinely running).
        setContainerRunActive(false);
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
          // Local mutation the in-flight history revalidation must not clobber.
          transcriptMutationSeqRef.current += 1;
          setChatMessages((prev) => [...prev, synthesized]);
          const segText = segments.reduce((acc, s) => acc + s.text.length, 0);
          turnAssistantLenRef.current += segText + (tailStream?.length ?? 0);
        }

        // Clear ALL live state so the next turn starts from a blank slate.
        setChatStream(null);
        chatStreamRunIdRef.current = null;
        chatStreamStartedAtRef.current = null;
        streamCommittedOffsetRef.current = 0;
        streamCumulativeRef.current = null;
        setChatStreamThinking(null);
        thinkingCumulativeRef.current = null;
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
              onChatCompletedRef.current?.();
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
          addCappedRunId(drainedRunIdsRef.current, runId);
          drainQueueRef.current();
        }
        return;
      }

      if (msg.type === "error") {
        // Defensive: we no longer run a server-side serialize gate. Concurrency
        // is handled the upstream way — client-side queue + observed-run
        // adoption + the container's per-session write-lock — so the backend
        // never emits `agent_busy`. If a stray one ever arrives (e.g. a
        // mid-deploy skew with an old backend), treat it as a benign no-op: it
        // carries NO runId, and the generic error path below would (because
        // !msg.runId) clear clientRunIdRef and isStreaming, WIPING the live turn.
        if (msg.code === "agent_busy") {
          return;
        }

        // A steer injection that failed is NOT terminal for the primary run —
        // OpenClaw keeps working. Surface a notice but do NOT tear down the
        // live turn (the generic no-runId path below would wipe it).
        if (msg.code === "steer_failed") {
          // Non-terminal: the primary run keeps going. Toast the notice instead
          // of the full-view `error` banner (which would hide the live stream).
          notifyApiError(new Error(friendlyError(msg.message)), "Failed to steer message", {
            dedupeKey: "chat-steer",
          });
          return;
        }

        // A trial container that scaled to zero answers a chat send with a
        // benign `container_waking` code instead of a hard failure. Kick ONE
        // /container/status revalidation so the sibling ProvisioningStepper —
        // which subscribes to the same SWR key — sees the now-non-ready phase
        // and flips to its cold-start UI (isRegressingFromReady), then back to
        // ready on the container's status_change:HEALTHY push; the status
        // endpoint's ensure_user_ready is what actually restarts the stopped
        // box. The frame still flows through the terminal-error path below to
        // release the busy gate + drain the queue (it carries no runId, exactly
        // like the no-container / billing-block transport errors), but is
        // rendered as CALM plain text — no errorCode (so MessageList doesn't
        // upgrade it to a red ChatErrorCard) and no top-level error banner — so
        // the user sees "Waking your agent…", not "Failed to generate".
        const isWaking = msg.code === "container_waking";
        if (isWaking) {
          swrMutateRef.current("/container/status");
        }

        // Genuine terminal error (past the agent_busy / steer_failed
        // non-terminal no-ops above) — belt-and-suspenders close, same as
        // the run_ended / done branches.
        setContainerRunActive(false);

        const displayError = friendlyError(msg.message);
        // A user-initiated Stop is not a failure — treat it like the waking
        // notice: calm plain text, no errorCode.
        const isCalmNotice = isWaking || ABORTED_PATTERN.test(msg.message);
        const errorCode = isCalmNotice ? undefined : msg.code;
        // Append an assistant-role message. A calm notice (waking / aborted)
        // renders as plain markdown (no "Error: " prefix, no errorCode); a
        // genuine error carries the "Error: " prefix + errorCode that
        // MessageList upgrades to a typed ChatErrorCard.
        const errMsg: HistoryMessage = {
          role: "assistant",
          content: [
            { type: "text", text: isCalmNotice ? displayError : `Error: ${displayError}` },
          ],
          timestamp: Date.now(),
          runId: msg.runId,
          errorCode,
        };
        // Local mutation the in-flight history revalidation must not clobber
        // (the error bubble exists only client-side — a server snapshot can
        // never contain it).
        transcriptMutationSeqRef.current += 1;
        setChatMessages((prev) => [...prev, errMsg]);
        // Clear ALL live state — including tool state. Without clearing
        // tools, a tool that was still `running` when the error arrived
        // would keep the stillRunning check true forever and block
        // setIsStreaming(false), leaving the chat stuck in streaming mode.
        setChatStream(null);
        chatStreamRunIdRef.current = null;
        streamCommittedOffsetRef.current = 0;
        streamCumulativeRef.current = null;
        setChatStreamThinking(null);
        thinkingCumulativeRef.current = null;
        chatStreamThinkingRunIdRef.current = null;
        setChatStreamSegments([]);
        toolStreamByIdRef.current.clear();
        toolStreamOrderRef.current = [];
        setChatToolMessages([]);
        // Did THIS error end the active run? A terminal agent error carries
        // the run's runId (the no-runId agent_busy notice was handled and
        // returned above). Capture state before clearing so we can drain.
        const endedActiveRun =
          !!msg.runId && msg.runId === clientRunIdRef.current;
        const gateWasArmed = clientRunIdRef.current !== null;
        if (!msg.runId || msg.runId === clientRunIdRef.current) {
          clientRunIdRef.current = null;
        }
        // Tombstone cleanup happens in the short-circuit at the top of
        // this handler; by here, msg.runId is never in the set.
        if (clientRunIdRef.current === null) {
          setIsStreaming(false);
        }

        // Drain the queue if this error RELEASED the busy gate, so a follow-up
        // the user queued while the failed send was pending isn't stranded.
        // Two release paths:
        //  - terminal agent error WITH a matching runId (endedActiveRun): dedupe
        //    by runId so the trailing `run_ended {outcome:"error"}` doesn't
        //    double-drain.
        //  - no-runId transport/gate error (no container, billing block, send
        //    failure) that just cleared clientRunIdRef: there is no runId to key
        //    a dedupe on, and no trailing run_ended will arrive, so drain here.
        if (endedActiveRun && !drainedRunIdsRef.current.has(msg.runId!)) {
          addCappedRunId(drainedRunIdsRef.current, msg.runId!);
          drainQueueRef.current();
        } else if (!msg.runId && gateWasArmed && clientRunIdRef.current === null) {
          drainQueueRef.current();
        }
        return;
      }

      // heartbeat / update_available — no chat-state mutation.
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [onChatMessage]);

  // ---- Send message ----

  // Hoisted so both sendMessage and the drain can call it. dispatchNow
  // assumes the busy-gate has already been checked. Returns true on a
  // successful wire send, false if the send threw.
  //
  // `queueItem` distinguishes a drain-originated send from a direct typed
  // send. For a drain failure we re-queue the item as `failed` (so it stays
  // visible and retryable) and roll back the optimistic user bubble + gate,
  // instead of appending an "Error:" bubble to history. Mirrors upstream's
  // flushChatQueue treating a failed send via sendState on the queue item.
  const dispatchNow = useCallback(
    async (
      message: string,
      opts?: { queueItem?: QueuedMessage },
    ): Promise<boolean> => {
      const agentId = agentIdRef.current;
      if (!agentId || !isConnected) {
        console.warn(
          `[chat] dispatch refused pre-wire: hasAgent=${!!agentId} connected=${isConnected} fromQueue=${!!opts?.queueItem}`,
        );
        if (!agentId && !opts?.queueItem) return false;
        if (!isConnected) {
          notifyApiError(new Error("Not connected. Please wait and try again."), "Not connected", {
            dedupeKey: "chat-send",
          });
        }
        if (opts?.queueItem) {
          // The drain already removed this item — dropping it here would
          // lose the user's message with no trace. Park it back instead:
          // disconnected → waiting-reconnect (replayed on the rising edge),
          // no agent → failed (user can retry once an agent is selected).
          const reparked: QueuedMessage = {
            ...opts.queueItem,
            sendState: !isConnected ? "waiting-reconnect" : "failed",
            sendError: !agentId ? "No agent selected" : undefined,
          };
          setQueuedMessages((prev) => [reparked, ...prev]);
        }
        return false;
      }

      const newClientRunId = crypto.randomUUID();
      clientRunIdRef.current = newClientRunId;

      const userMsg: HistoryMessage = {
        role: "user",
        content: [{ type: "text", text: message }],
        timestamp: Date.now(),
      };
      // Local mutation the in-flight history revalidation must not clobber.
      transcriptMutationSeqRef.current += 1;
      setChatMessages((prev) => [...prev, userMsg]);
      setIsStreaming(true);

      turnStartedAtRef.current = Date.now();
      turnAssistantLenRef.current = 0;
      capture("chat_message_sent", {
        agent_id: agentId,
        message_length: message.length,
      });
      emitStep("chat");

      try {
        console.info(`[chat] dispatch runId=${newClientRunId} fromQueue=${!!opts?.queueItem}`);
        sendChat(agentId, message, newClientRunId, activeSessionIdRef.current ?? undefined);
        return true;
      } catch (err) {
        console.warn(
          `[chat] dispatch failed runId=${newClientRunId}: ${err instanceof Error ? err.message : err}`,
        );
        const errorMessage = friendlyError(
          err instanceof Error ? err.message : "Failed to send message",
        );
        // Roll back the optimistic turn state regardless of origin.
        setChatMessages((prev) => prev.filter((m) => m !== userMsg));
        setIsStreaming(false);
        clientRunIdRef.current = null;

        if (opts?.queueItem) {
          // Drain failure: re-queue the item as failed so the user can
          // retry it. No "Error:" history bubble — the failure surfaces on
          // the queue item itself.
          const failed: QueuedMessage = {
            ...opts.queueItem,
            sendState: "failed",
            sendError: errorMessage,
          };
          setQueuedMessages((prev) => [failed, ...prev]);
        } else {
          // Direct typed send: toast the transient failure (ChatInput is the
          // retry affordance). The transcript "Error:" bubble below stays as
          // the in-stream record.
          notifyApiError(err, errorMessage, { dedupeKey: "chat-send" });
          const errMsg: HistoryMessage = {
            role: "assistant",
            content: [{ type: "text", text: `Error: ${errorMessage}` }],
            timestamp: Date.now(),
          };
          setChatMessages((prev) => [...prev, errMsg]);
        }
        return false;
      }
    },
    [sendChat, isConnected, setIsStreaming, setChatMessages],
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
    // Drain the first PLAIN queued send. Skip items the user must act on
    // (failed → Retry, waiting-reconnect → handled on the reconnect edge).
    const next = queuedMessagesRef.current.find((m) => !m.sendState);
    if (!next) return;
    setQueuedMessages((q) => q.filter((m) => m.id !== next.id));
    // On a dispatch failure, dispatchNow re-queues `next` as failed.
    void dispatchNow(next.text, { queueItem: next });
  }, [isConnected, dispatchNow]);

  // Keep the handler-visible drain ref pointed at the latest drainQueue so
  // the once-registered onChatMessage handler invokes the current closure
  // (fresh isConnected / dispatchNow) without re-binding.
  useEffect(() => {
    drainQueueRef.current = drainQueue;
  }, [drainQueue]);

  // Re-attempt a failed queued send. Pulls the item out of the queue,
  // clears its failed state, and:
  //   - disconnected → re-park as waiting-reconnect (the reconnect edge
  //     will replay it);
  //   - busy (a run is active) → re-park as a plain queued send (the
  //     server run-ended drain will pick it up);
  //   - free + connected → dispatch immediately (re-queued as failed again
  //     if the send throws).
  // Mirrors upstream OpenClaw web UI's retryQueuedChatMessage
  // (~/Desktop/openclaw/ui/src/ui/chat/chat-queue.ts:914).
  const retryQueuedMessage = useCallback(
    (id: string) => {
      const item = queuedMessagesRef.current.find((m) => m.id === id);
      if (!item) return;

      if (!isConnected) {
        setQueuedMessages((prev) =>
          prev.map((m) =>
            m.id === id
              ? { ...m, sendState: "waiting-reconnect", sendError: undefined }
              : m,
          ),
        );
        return;
      }

      if (clientRunIdRef.current !== null) {
        // Still busy — clear the failed flag and let the normal drain take it.
        setQueuedMessages((prev) =>
          prev.map((m) =>
            m.id === id ? { ...m, sendState: undefined, sendError: undefined } : m,
          ),
        );
        return;
      }

      // Free + connected → dispatch now. Pull it out first; dispatchNow
      // re-queues it as failed if the send throws again.
      const cleaned: QueuedMessage = {
        ...item,
        sendState: undefined,
        sendError: undefined,
      };
      setQueuedMessages((prev) => prev.filter((m) => m.id !== id));
      void dispatchNow(cleaned.text, { queueItem: cleaned });
    },
    [isConnected, dispatchNow],
  );

  // Refs to the latest clear/cancel callbacks so sendMessage (defined above
  // them) can invoke the current closures for the local /clear and /redirect
  // commands without a forward-reference / stale closure — same pattern as
  // drainQueueRef. Populated by the effects beside each definition below.
  const clearMessagesRef = useRef<() => void>(() => {});
  const cancelMessageRef = useRef<() => Promise<void>>(async () => {});

  const sendMessage = useCallback(
    async (message: string): Promise<void> => {
      if (!agentIdRef.current) {
        throw new Error("No agent selected");
      }

      const trimmed = message.trim();
      if (!trimmed) return;

      const key = agentIdRef.current
        ? `${agentIdRef.current}:${sessionName}:${activeSessionIdRef.current ?? ""}`
        : null;
      // Sending clears only the bootstrap hint — NOT _truncatedHistory: the
      // send doesn't refetch history, so the loaded window is still missing
      // its trimmed older messages and the banner must stay.
      if (key) _needsBootstrap.delete(key);

      // Side-channel `/btw` (or `/side`): a detached, non-blocking aside that
      // runs alongside the main turn rather than queuing behind it. Send it
      // IMMEDIATELY — even while a run is active — and do NOT touch
      // clientRunIdRef / isStreaming (a side run is detached, it has no claim
      // on the primary run's busy gate). The FULL original text including the
      // `/btw` prefix goes on the wire: the container detects the prefix and
      // runs it as a non-blocking side run server-side.
      //
      // We MINT the side run's runId here (sent as clientRunId → backend
      // idempotencyKey → the runId every side event carries) and record it in
      // sideRunIdsRef so the event handler segregates the side run's
      // stream/terminal events from the primary turn. Mirrors upstream
      // sendDetachedChatMessage, which generates the runId client-side
      // (controllers/chat.ts) and the client tracks it (TUI noteLocalBtwRunId).
      // The eventual side `run_ended` is dropped by that segregation, so it can
      // never drain the primary queue.
      //
      // Known limitation: a `/btw` typed while DISCONNECTED does not match here
      // (no live socket to mint+track a side run against), so it falls through
      // to the waiting-reconnect queue below and replays as an ordinary send on
      // reconnect — losing its detached framing client-side (the container still
      // detects the `/btw` prefix server-side and runs it as a side run). This
      // is accepted while `/btw` lacks a dedicated side-result surface: detached
      // answers aren't rendered in the web UI yet regardless, and the trigger
      // (compose a `/btw` precisely during a transient disconnect) is narrow.
      if (/^\/(btw|side)(?::|\s|$)/i.test(trimmed) && isConnected && agentIdRef.current) {
        const sideRunId = crypto.randomUUID();
        addCappedRunId(sideRunIdsRef.current, sideRunId);
        send({
          type: "agent_chat",
          agent_id: agentIdRef.current,
          message: trimmed,
          side: true,
          clientRunId: sideRunId,
          ...(activeSessionIdRef.current ? { session_id: activeSessionIdRef.current } : {}),
        });
        return;
      }

      // `/clear` and `/redirect` are control-ui-only in OpenClaw — there is no
      // server handler for either, so we implement them locally (before the
      // disconnected / busy gates below, which don't apply to a local-only
      // command nor to an explicit redirect).
      //
      // `/clear` wipes the local transcript and sends NOTHING to the server.
      const trimmedLower = trimmed.toLowerCase();
      if (trimmedLower === "/clear") {
        clearMessagesRef.current();
        return;
      }
      // `/redirect <msg>` aborts the active run (the existing cancel path) and
      // resends the remainder as a fresh message — interrupting rather than
      // queuing behind the in-flight run.
      if (/^\/redirect\s+/i.test(trimmed)) {
        const rest = trimmed.replace(/^\/redirect\s+/i, "");
        await cancelMessageRef.current();
        if (rest) await dispatchNow(rest);
        return;
      }
      // `/reset`, `/compact`, and `/new` mutate server-side session state. If a
      // run is still streaming, abort it FIRST (mirrors the `/redirect` path) —
      // dispatching the command without aborting would start a concurrent run
      // while the orphan keeps burning tokens server-side and its late
      // chunk/done events repaint a ghost bubble after the refetch. Then let the
      // command reach the server and invalidate the cached transcript + refetch
      // history so the displayed messages match the (reset/compacted) session.
      if (/^\/(reset|compact|new)(\s|$)/i.test(trimmed)) {
        await cancelMessageRef.current();
        await dispatchNow(trimmed);
        invalidateHistoryAndRefetch();
        return;
      }

      // Disconnected: park the send as waiting-reconnect instead of
      // dropping it. The reconnect rising-edge effect replays it (and
      // resumes the normal drain) once the socket is back. Mirrors upstream
      // markQueuedChatSendsWaitingForReconnect (chat-queue.ts:848).
      if (!isConnected) {
        console.info("[chat] send parked: gateway disconnected, queued as waiting-reconnect");
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
        console.info(`[chat] send queued: run ${clientRunIdRef.current} still in flight`);
        const id = `queued-${crypto.randomUUID()}`;
        setQueuedMessages((prev) => [
          ...prev,
          { id, text: trimmed, createdAt: Date.now() },
        ]);
        return;
      }

      await dispatchNow(trimmed);
    },
    [isConnected, sessionName, dispatchNow, send, invalidateHistoryAndRefetch],
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

    // `send` returns false when the socket isn't actually open — the
    // stale-isConnected race where the WS closed but React hasn't re-rendered
    // isConnected=false yet. Because cancel is server-reconciled (we wait for a
    // terminal rather than tearing down locally), a silently-dropped abort here
    // would hang the chat. Park the intent so the reconnect replay fires it.
    const delivered = send({
      type: "agent_abort",
      agent_id: agentIdRef.current,
      ...(activeRunId ? { runId: activeRunId } : {}),
      ...(activeSessionIdRef.current ? { session_id: activeSessionIdRef.current } : {}),
    });
    if (!delivered) {
      pendingAbortRef.current = {
        agentId: agentIdRef.current,
        runId: activeRunId,
      };
    }
  }, [isStreaming, isConnected, send]);

  // Keep cancelMessageRef pointed at the latest cancelMessage so sendMessage's
  // /redirect path invokes the current closure (fresh isStreaming/isConnected).
  useEffect(() => {
    cancelMessageRef.current = cancelMessage;
  }, [cancelMessage]);

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
        ...(activeSessionIdRef.current ? { session_id: activeSessionIdRef.current } : {}),
      });
    }

    const waiting = queuedMessagesRef.current.filter(
      (m) => m.sendState === "waiting-reconnect",
    );
    if (waiting.length > 0) {
      // Strip the waiting-reconnect flag on ALL parked sends so they become
      // plain queued sends (UI no longer shows "Waiting for reconnect").
      setQueuedMessages((prev) =>
        prev.map((m) =>
          m.sendState === "waiting-reconnect"
            ? { ...m, sendState: undefined, sendError: undefined }
            : m,
        ),
      );
      // Resume sending. If the gate is free, dispatch the first parked send
      // now (drainQueue can't see it yet — the strip setState above hasn't
      // committed to queuedMessagesRef). Its server run-ended re-invokes the
      // normal drain for the rest, which by then are plain queued sends.
      if (clientRunIdRef.current === null) {
        const head = waiting[0];
        const cleaned: QueuedMessage = {
          ...head,
          sendState: undefined,
          sendError: undefined,
        };
        setQueuedMessages((prev) => prev.filter((m) => m.id !== head.id));
        void dispatchNow(cleaned.text, { queueItem: cleaned });
      }
    }
  }, [isConnected, send, dispatchNow]);

  // ---- Drop stale server-truth busy on disconnect ----
  //
  // containerRunActive is server truth relayed over the WS — while
  // disconnected we can't receive its `run_active:false` companion, so a
  // `true` set before the drop must not persist as a phantom "busy"
  // indefinitely. No existing effect resets general live state purely on
  // the isConnected transition (the effect above is reconnect-specific
  // replay logic), so this is a dedicated one. Fires on drop; the
  // subsequent reconnect will pick up a fresh run_active from the server
  // if the container is still genuinely running.
  useEffect(() => {
    if (!isConnected) setContainerRunActive(false);
  }, [isConnected]);

  // ---- Clear messages ----

  const clearMessages = useCallback(() => {
    // Late events for the cleared run shouldn't repopulate the now-empty
    // chat — mark its runId as cancelled so the handler drops them.
    const activeRunId = clientRunIdRef.current;
    if (activeRunId) cancelledRunIdsRef.current.add(activeRunId);
    // Local mutation the in-flight history revalidation must not clobber —
    // a background refetch resolving after a /clear would un-clear the view.
    transcriptMutationSeqRef.current += 1;
    setChatMessages([]);
    setChatStream(null);
    chatStreamRunIdRef.current = null;
    streamCommittedOffsetRef.current = 0;
    streamCumulativeRef.current = null;
    setChatStreamThinking(null);
    thinkingCumulativeRef.current = null;
    chatStreamThinkingRunIdRef.current = null;
    setChatStreamSegments([]);
    setChatToolMessages([]);
    // Ephemeral /btw side cards are part of the local view — wipe them too.
    setBtwResults([]);
    toolStreamByIdRef.current.clear();
    toolStreamOrderRef.current = [];
    const key = agentIdRef.current
      ? `${agentIdRef.current}:${sessionName}:${activeSessionIdRef.current ?? ""}`
      : null;
    if (key) _messageCache.delete(key);
    setIsStreaming(false);
    // Drop the server-truth busy bit too (agent switch routes through here):
    // the old scope's run_active:false is demux-dropped once the agent changes,
    // so leaving it set would strand the spinner on the new agent's chat.
    setContainerRunActive(false);
    clientRunIdRef.current = null;
    // Same reasoning as cancelMessage — a post-approval continuation
    // must not be adopted as the new active run after a clear.
    resumeExpectedRef.current = false;
  }, [
    sessionName,
    setChatMessages,
    setIsStreaming,
    setChatStream,
    setChatStreamThinking,
    setChatStreamSegments,
    setChatToolMessages,
  ]);

  // Keep clearMessagesRef pointed at the latest clearMessages so sendMessage's
  // /clear path invokes the current closure (fresh sessionName-keyed cache).
  useEffect(() => {
    clearMessagesRef.current = clearMessages;
  }, [clearMessages]);

  // ---- External interface ----

  // Compute render output from state. memo for stability.
  const renderItems = useMemo<ChatRenderItem[]>(() => {
    const items = buildChatItems({
      chatMessages,
      chatStream,
      chatStreamRunId: chatStreamRunIdRef.current,
      chatStreamThinking,
      chatStreamThinkingRunId: chatStreamThinkingRunIdRef.current,
      chatStreamSegments,
      chatToolMessages,
      sessionKey: buildChatSessionKey(agentId ?? "", sessionName, activeSessionId),
    });
    // Append ephemeral `/btw` side-result cards at the tail. They live in
    // transient state only (never in chatMessages / _messageCache), so they
    // disappear on refresh — matching OpenClaw's "Not saved to chat history".
    for (const b of btwResults) {
      items.push({
        kind: "btw",
        key: `btw:${b.id}`,
        question: b.question,
        text: b.text,
        ...(b.isError ? { isError: true } : {}),
      });
    }
    return items;
  }, [chatMessages, chatStream, chatStreamThinking, chatStreamSegments, chatToolMessages, agentId, sessionName, activeSessionId, btwResults]);

  const renderGroups = useMemo<RenderGroup[]>(() => groupBySide(renderItems), [renderItems]);

  const historyTruncated = !!(cacheKey && _truncatedHistory.has(cacheKey));

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
    busy: isStreaming || containerRunActive,
    sendMessage,
    cancelMessage,
    clearMessages,
    isConnected,
    isLoadingHistory,
    needsBootstrap,
    historyTruncated,
    queuedMessages,
    removeQueuedMessage,
    clearQueue,
    steerMessage,
    retryQueuedMessage,
    activeSessionId,
    newSession,
    selectSession,
  };
}
