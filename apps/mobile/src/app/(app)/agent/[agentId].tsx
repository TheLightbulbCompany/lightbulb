import { useAuth, useUser } from "@clerk/clerk-expo";
import Feather from "@expo/vector-icons/Feather";
import {
  accumulateStreamText,
  isRetryableRpcError,
  rpcRetryDelayMs,
} from "@isol8/shared";
import { randomUUID } from "expo-crypto";
import * as DocumentPicker from "expo-document-picker";
import * as ImagePicker from "expo-image-picker";
import { router, Stack, useFocusEffect, useLocalSearchParams } from "expo-router";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ActionSheetIOS,
  ActivityIndicator,
  Alert,
  Animated,
  FlatList,
  KeyboardAvoidingView,
  Platform,
  Pressable,
  StyleSheet,
  Text,
  TextInput,
  View,
} from "react-native";

import { AgentHead } from "../../../components/chat/AgentHead";
import { AttachmentChips } from "../../../components/chat/AttachmentChips";
import { ChatErrorCard } from "../../../components/chat/ChatErrorCard";
import type { ChatItem } from "../../../components/chat/chatItems";
import { EmptyState } from "../../../components/chat/EmptyState";
import { MessageMarkdown } from "../../../components/chat/MessageMarkdown";
import { SessionsSheet } from "../../../components/chat/SessionsSheet";
import { TimelineSkeleton } from "../../../components/chat/TimelineSkeleton";
import { ToolPill, type ToolPillStatus } from "../../../components/chat/ToolPill";
import { usePulse } from "../../../components/chat/usePulse";
import { colors, fonts, radii, spacing } from "../../../config/theme";
import { setCurrentAgentId, setCurrentSessionId } from "../../../lib/activeAgent";
import {
  buildUploadNotice,
  filterOversized,
  uploadAttachments,
  MAX_ATTACHMENTS,
  type PendingAttachment,
} from "../../../lib/attachments";
import { projectHistoryToChatItems } from "../../../lib/chatHistory";
import { useNavParamConsumer } from "../../../lib/useNavParamConsumer";
import { buildChatSessionKey } from "../../../lib/sessionKey";
import { useGateway } from "../../../providers/gateway";

/**
 * Bounds for silently riding out a RETRYABLE chat.history failure: BOTH a
 * wall-clock window and an attempt cap, because either alone is wrong.
 *
 * An attempt count alone cannot serve both delays that reach this path —
 * OpenClaw's index rebuild asks for ~250ms, a waking container for 3000ms —
 * so "3 attempts" is either ~750ms of patience or ~9s depending on who
 * answered. A window alone removes the only bound on REQUEST COUNT: at the
 * gateway's 250ms a 60s window is ~240 full WS→backend→pool→container round
 * trips from one screen, which is a self-inflicted retry storm aimed at a box
 * that is already unhealthy. Whichever bound trips first wins.
 */
const HISTORY_RETRY_WINDOW_MS = 60_000;
const HISTORY_RETRY_MAX_ATTEMPTS = 8;

// A user-initiated Stop is delivered as an `error` frame ("Agent run was
// cancelled") — a normal outcome, not a failure (web ABORTED_PATTERN parity).
const ABORTED_PATTERN = /Agent run was cancelled/i;

// Known transport/infra error strings → friendly copy (web useAgentChat
// ERROR_PATTERNS parity). Everything else passes through verbatim.
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

/**
 * Slim reconnecting banner shown above the composer while the gateway
 * socket is down (spec §4): amber well with a pulsing dot.
 */
function ReconnectingBanner() {
  const pulse = usePulse(true, 0.3, 700);
  return (
    <View style={styles.banner}>
      <Animated.View style={[styles.bannerDot, { opacity: pulse }]} />
      <Text style={styles.bannerText}>Reconnecting…</Text>
    </View>
  );
}

/**
 * Phase 2 chat: streams one conversation with one agent over the shared
 * gateway, hydrated from `chat.history` and scoped to an active session
 * (General or a named UUID session — see .superpowers/sdd/
 * w1-sessions-contract.md). Protocol handling follows the web contract:
 *
 * - `chunk` frames arrive as append deltas (`append: true`), folded into
 *   the run buffer via accumulateStreamText, or as SEGMENT-scoped resyncs
 *   (no `append` — the current assistant message only), which the chunk
 *   handler rebuilds as frozen-prefix + segment text itself (see
 *   packages/shared streamText docstring; web useAgentChat still folds
 *   resyncs through the cumulative model and carries the latent chop).
 *   Bubbles REPLACE their text from that buffer, and a per-run committed
 *   offset splits the cumulative stream into per-segment bubbles when
 *   tool calls interleave.
 * - `run_ended` (not `done`) releases the busy gate — `done` also fires on
 *   interim tool-approval brackets.
 *
 * Presentation follows the web design spec §1/§2/§7: user text in a cream
 * bubble on the right, assistant markdown flat on the page under an
 * AgentHead whose glyph pulses while the run is active.
 */
export default function AgentChatRoute() {
  // This route is a PERSISTED tab sibling (href: null): navigating to a
  // different agent re-params the same mounted component, so every piece of
  // conversation state (items, history, session, run refs) would carry over
  // and paint the PREVIOUS agent's transcript until something replaced it —
  // the stale-conversation flash. Keying by agentId remounts the screen per
  // agent, which resets all of it structurally; no cross-agent reset effect
  // can be forgotten or early-return its way into a no-op.
  const { agentId } = useLocalSearchParams<{ agentId: string }>();
  return <AgentChat key={agentId ?? "none"} />;
}

function AgentChat() {
  const {
    agentId,
    name,
    newSession: newSessionParam,
    sessionId: sessionIdParam,
    sessionLabel: sessionLabelParam,
  } = useLocalSearchParams<{
    agentId: string;
    name?: string;
    newSession?: string;
    sessionId?: string;
    sessionLabel?: string;
  }>();
  // Publish the focused agent so the push notification handler suppresses the
  // banner for a reply the user is actively watching stream in (activeAgent.ts).
  useFocusEffect(
    useCallback(() => {
      setCurrentAgentId(agentId ?? null);
      // Republish the session on refocus (setCurrentAgentId(null) on blur
      // clears both halves); switchSession keeps it current while focused.
      setCurrentSessionId(activeSessionIdRef.current);
      return () => setCurrentAgentId(null);
    }, [agentId]),
  );
  const gateway = useGateway();
  // Session keys embed the Clerk user id (contract §1); history/session
  // fetches wait for Clerk to load it.
  const { user } = useUser();
  const userId = user?.id ?? null;
  // Clerk hands out a NEW getToken reference every render (teamsData.ts
  // lesson) — pin it in a ref so the upload path always reads the current one
  // without threading getToken through any dependency array.
  const { getToken } = useAuth();
  const getTokenRef = useRef(getToken);
  getTokenRef.current = getToken;
  const [items, setItems] = useState<ChatItem[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  // Presentational-only composer state (spec §4): focus ring.
  const [inputFocused, setInputFocused] = useState(false);
  // ---- Attachments (web ChatInput/AgentChatWindow parity). ----
  const [attachments, setAttachments] = useState<PendingAttachment[]>([]);
  const [uploading, setUploading] = useState(false);
  // Oversize-rejection banner: web parity — shows the same message, auto-
  // dismisses after 5s, manually dismissible.
  const [sizeError, setSizeError] = useState<string | null>(null);
  const sizeErrorTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // ---- Sessions (contract §1): null == the legacy "General" session. ----
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null);
  const [activeSessionLabel, setActiveSessionLabel] = useState<string | null>(null);
  const [sheetOpen, setSheetOpen] = useState(false);
  // Ref twin of activeSessionId for the frame handler's session demux —
  // keeps the subscription effect's dep list untouched.
  const activeSessionIdRef = useRef<string | null>(null);
  // Locally-minted session ids with no server-side transcript yet (session
  // creation is implicit — the session materializes on the first agent_chat
  // carrying its id, contract §1). History fetch skips these.
  const freshSessionIdsRef = useRef<Set<string>>(new Set());
  // ---- History hydration state (contract §2). ----
  const [historyItems, setHistoryItems] = useState<ChatItem[]>([]);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [historyError, setHistoryError] = useState<string | null>(null);
  const [historyNonce, setHistoryNonce] = useState(0);
  // Deadline for silently riding out RETRYABLE history failures (see the
  // fetch's catch). `null` means "no run in progress — start one on the next
  // retryable failure". Cleared on a SUCCESSFUL fetch, a session switch, and
  // the card's Retry; deliberately NOT extended by the auto-retry itself,
  // which re-enters through the same nonce and would otherwise never give up
  // on a genuinely stuck container.
  const historyRetryUntilRef = useRef<number | null>(null);
  const historyRetryCountRef = useRef(0);
  // Set only when the effect is re-entered BY the scheduled auto-retry, so
  // every other re-entry (mount, session change, reconnect, manual Retry)
  // counts as a fresh ask and opens a new window. Without it an expired
  // window stayed expired for the life of the session: a reconnect landing on
  // OpenClaw's ~250ms index rebuild skipped the silent retry entirely and
  // painted the card — the very regression reset-on-success exists to avoid.
  const historyRetryReentryRef = useRef(false);
  const historyRetryTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const activeRunIdRef = useRef<string | null>(null);
  // Runs whose run_ended already arrived — a stray late frame for one of
  // these must not re-adopt it as the active run (web drainedRunIds parity).
  const drainedRunIdsRef = useRef<Set<string>>(new Set());
  // Cumulative-stream bookkeeping for the active run: how many chars of the
  // cumulative text earlier segments already display, the current segment
  // ordinal (keys stay unique when tools split the stream), and the last
  // cumulative length seen (becomes the offset when a tool interrupts).
  const committedOffsetRef = useRef(0);
  const segmentRef = useRef(0);
  const lastCumulativeLenRef = useRef(0);
  // Reconstructed cumulative text of the run — chunk frames arrive as append
  // deltas or full-text resyncs (see accumulateStreamText); this buffer
  // always holds the full text the legacy cumulative protocol delivered.
  const cumulativeTextRef = useRef<string | null>(null);
  const listRef = useRef<FlatList<ChatItem>>(null);
  const inputRef = useRef<TextInput>(null);

  useEffect(() => {
    return gateway.onChatMessage((msg) => {
      if (!("agent_id" in msg) || msg.agent_id !== agentId) return;
      // Session demux (contract §5 layer 2), additive above the run gating:
      // the backend stamps session_id on EVERY named-session frame and NEVER
      // on General frames. So with a named session active, only frames
      // carrying ITS id belong here (an untagged frame is a General run
      // leaking); with General active, only untagged frames do.
      const frameSessionId =
        "session_id" in msg && typeof msg.session_id === "string"
          ? msg.session_id
          : null;
      // UNTAGGED error frames are EXEMPT from the session demux (mirrors
      // the web's layer-1 exception): backend gate-level errors (no
      // container / starting / provision blocked) carry agent_id but NO
      // session_id and no run_ended follows — dropping them on a named
      // session would leave the user staring at nothing with `busy` wedged
      // true forever. A session-tagged error still demuxes normally.
      const isUntaggedError = msg.type === "error" && frameSessionId === null;
      if (frameSessionId !== activeSessionIdRef.current && !isUntaggedError)
        return;

      // Server-truth busy signal (backend forwards OpenClaw sessions.changed;
      // carries no runId). active:true marks busy before an external run's
      // first frame arrives; active:false releases a latch a lost terminal
      // frame stranded (web run_active parity).
      if (msg.type === "run_active") {
        if (msg.active) {
          setBusy(true);
        } else {
          activeRunIdRef.current = null;
          setBusy(false);
        }
        return;
      }

      const runId = "runId" in msg ? msg.runId : undefined;
      // Adopt an externally-started run when this screen has none — a run
      // started from web, cron, or a previous app session must render here
      // and hold the composer busy, instead of the old false-idle (frames
      // silently dropped, sends dispatched into the live run; there is no
      // server-side chat gate). Ended runs must not re-arm the gate.
      if (
        runId &&
        activeRunIdRef.current === null &&
        !drainedRunIdsRef.current.has(runId) &&
        msg.type !== "run_ended"
      ) {
        activeRunIdRef.current = runId;
        committedOffsetRef.current = 0;
        segmentRef.current = 0;
        lastCumulativeLenRef.current = 0;
        cumulativeTextRef.current = null;
        setBusy(true);
      }
      // Only render frames for the active run — other concurrent runs for the
      // same agent stay out of this transcript.
      if (runId && runId !== activeRunIdRef.current) return;

      if (msg.type === "chunk") {
        const segmentId = `${runId}-s${segmentRef.current}`;
        // Non-append resync frames (first frame after a reconnect, replace
        // events, the chat.final backstop) carry the CURRENT assistant
        // message's full text — upstream resets its accumulator at every
        // message_start, i.e. after every tool call — NOT the run-cumulative
        // text this buffer holds. Rebuild as frozen-prefix + segment text;
        // treating it as run-cumulative chopped the live bubble at the
        // committed offset ("disappearing sentences"). With no committed
        // prefix this degrades to the plain replace it always was.
        const cumulative = msg.append
          ? accumulateStreamText(cumulativeTextRef.current, msg)
          : (cumulativeTextRef.current ?? "").slice(0, committedOffsetRef.current) +
            msg.content;
        cumulativeTextRef.current = cumulative;
        const segmentText = cumulative.slice(committedOffsetRef.current);
        lastCumulativeLenRef.current = cumulative.length;
        setItems((prev) => {
          const idx = prev.findIndex((it) => it.id === segmentId);
          if (idx >= 0) {
            const next = [...prev];
            next[idx] = { ...next[idx], text: segmentText };
            return next;
          }
          return [
            ...prev,
            // runId stamp: lets a later history refetch prune this run once
            // the seeded transcript covers it (dedupe, contract §5).
            { id: segmentId, role: "assistant", text: segmentText, streaming: true, runId },
          ];
        });
      } else if (msg.type === "tool_start") {
        // A tool call splits the stream: freeze the current segment, and
        // point the next segment past everything already rendered.
        committedOffsetRef.current = lastCumulativeLenRef.current;
        segmentRef.current += 1;
        setItems((prev) => [
          ...prev.map((it) => (it.streaming ? { ...it, streaming: false } : it)),
          {
            id: `${msg.toolCallId ?? randomUUID()}`,
            role: "tool",
            text: `⚙ ${msg.tool}`,
            tool: msg.tool,
            args: msg.args,
            toolStatus: "running",
            // runId stamp for history-refetch dedupe (contract §5).
            runId,
          },
        ]);
      } else if (msg.type === "tool_end" || msg.type === "tool_error") {
        // Additive presentation wiring only — settles the matching pill's
        // status and attaches result text for the expansion panel. No
        // protocol bookkeeping: segment offsets/refs stay untouched
        // (tool_start owns the stream split).
        const status: ToolPillStatus =
          msg.type === "tool_error" ? "error" : "done";
        const resultText = (msg.result ?? [])
          .map((block) => block.text)
          .filter((t): t is string => typeof t === "string" && t.length > 0)
          .join("\n");
        setItems((prev) =>
          prev.map((it) =>
            it.role === "tool" && it.id === msg.toolCallId
              ? { ...it, toolStatus: status, result: resultText || it.result }
              : it,
          ),
        );
      } else if (msg.type === "error") {
        // Benign backend frame — another surface's send raced this one; not
        // terminal for anything this screen shows (web parity: no-op).
        if (msg.code === "agent_busy") {
          return;
        }
        // container_waking (common on mobile: backgrounding parks the socket,
        // trial containers scale to zero) and a user-initiated Stop are
        // NORMAL outcomes — render calm plain text, not a red error card
        // (web parity). Both are still terminal: release the gate below.
        const text = friendlyError(msg.message);
        const calm =
          msg.code === "container_waking" || ABORTED_PATTERN.test(msg.message);
        setItems((prev) => [
          ...prev,
          calm
            ? { id: randomUUID(), role: "assistant", text }
            : { id: randomUUID(), role: "error", text, code: msg.code },
        ]);
        // Additive gate release: gate-level errors are terminal but never
        // followed by a run_ended, so the error frame itself must release
        // the run gate — otherwise `busy` wedges true forever. Redundant
        // (but harmless) when a run_ended does follow.
        activeRunIdRef.current = null;
        setBusy(false);
      } else if (msg.type === "run_ended") {
        // Remember ended runs so a stray late frame can't re-adopt one.
        if (runId) drainedRunIdsRef.current.add(runId);
        setItems((prev) =>
          prev.map((it) => (it.streaming ? { ...it, streaming: false } : it)),
        );
        activeRunIdRef.current = null;
        setBusy(false);
      }
    });
  }, [gateway, agentId]);

  // ---- History hydration (contract §2) ----
  // Runs on mount, on session switch (activeSessionId), and on reconnect
  // (isConnected false→true re-fires the effect; the guard skips the false
  // edge). `sendReq` is referentially stable, so gateway state churn
  // (reconnectAttempt etc.) doesn't spam refetches.
  const { sendReq, isConnected } = gateway;
  useEffect(() => {
    // Deliberately leaves render state ALONE. Clearing historyLoading here
    // swapped the skeleton for the "Start a conversation" empty state on any
    // mid-load socket blip — over a transcript with weeks of messages, which
    // reads as data loss. The skeleton plus the reconnecting banner above the
    // composer is the honest pairing, and the effect refetches on the rising
    // edge.
    if (!agentId || !userId || !isConnected) return;
    if (activeSessionId && freshSessionIdsRef.current.has(activeSessionId)) {
      // Brand-new locally-minted session: no server transcript exists until
      // the first send, so there is nothing to fetch (contract §1).
      setHistoryItems([]);
      setHistoryLoading(false);
      setHistoryError(null);
      return;
    }
    if (historyRetryReentryRef.current) {
      historyRetryReentryRef.current = false;
    } else {
      historyRetryUntilRef.current = null;
      historyRetryCountRef.current = 0;
    }
    let cancelled = false;
    setHistoryLoading(true);
    setHistoryError(null);
    const sessionKey = buildChatSessionKey(agentId, userId, activeSessionId);
    sendReq("chat.history", { sessionKey, limit: 200 })
      .then((payload) => {
        if (cancelled) return;
        const projected = projectHistoryToChatItems(payload);
        setHistoryItems(projected.items);
        // Dedupe (contract §5): a run the seeded transcript now covers must
        // not double-render from the live list. Ended runs are authoritative
        // in history, so only the in-flight run keeps its live items — minus
        // a user bubble the server already persisted mid-run. Client-local
        // errors (no runId) always survive.
        setItems((prev) =>
          prev.filter((it) => {
            if (!it.runId) return true;
            // Active-run keep comes FIRST: a mid-run refetch (reconnect
            // while streaming) can return a snapshot that already contains
            // this run's persisted user message, so testing snapshot
            // coverage before this keep would drop the run's live frozen
            // segments and tool pills — unrecoverable content loss. Only
            // the user bubble the server already persisted dedupes away.
            if (it.runId === activeRunIdRef.current) {
              return !(
                it.role === "user" &&
                projected.lastUserText !== null &&
                it.text === projected.lastUserText
              );
            }
            // THEN prune by snapshot coverage: ended runs are authoritative
            // in history, so live items the fetched snapshot covers are
            // duplicates — and an ended run the snapshot somehow misses is
            // stale anyway. Either way the live copy goes.
            return false;
          }),
        );
        setHistoryLoading(false);
        // Window closes on success: the next transient in this session gets a
        // fresh one. Leaving it spent meant the SECOND rebuild in a long-lived
        // session went straight to the card this whole path exists to avoid.
        historyRetryUntilRef.current = null;
        historyRetryCountRef.current = 0;
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        // The responder describes its own transients: a rebuilding session
        // index answers retryable with retryAfterMs ~250ms, a waking container
        // with ~3000ms. Ride either out silently until the window closes — a
        // quarter-second condition must never reach a user as a red card
        // asking them to tap Retry, and a cold start must not give up a
        // quarter of the way in. The skeleton stays up across the wait, so it
        // reads as loading.
        if (isRetryableRpcError(err)) {
          const now = Date.now();
          if (historyRetryUntilRef.current === null) {
            historyRetryUntilRef.current = now + HISTORY_RETRY_WINDOW_MS;
          }
          const withinWindow = now < historyRetryUntilRef.current;
          const withinAttempts =
            historyRetryCountRef.current < HISTORY_RETRY_MAX_ATTEMPTS;
          if (withinWindow && withinAttempts) {
            historyRetryCountRef.current += 1;
            historyRetryReentryRef.current = true;
            historyRetryTimerRef.current = setTimeout(
              () => setHistoryNonce((n) => n + 1),
              rpcRetryDelayMs(err, 1000),
            );
            return;
          }
        }
        // Not latched: the effect re-fires on the next reconnect / session
        // visit, and the error card's Retry bumps historyNonce.
        setHistoryLoading(false);
        setHistoryError(
          err instanceof Error ? err.message : "Failed to load history",
        );
      });
    return () => {
      cancelled = true;
      if (historyRetryTimerRef.current) {
        clearTimeout(historyRetryTimerRef.current);
        historyRetryTimerRef.current = null;
      }
    };
  }, [agentId, userId, activeSessionId, isConnected, sendReq, historyNonce]);

  // ---- Session switching ----
  const switchSession = useCallback(
    (sessionId: string | null, label: string | null) => {
      if (sessionId === activeSessionIdRef.current) return;
      activeSessionIdRef.current = sessionId;
      setActiveSessionId(sessionId);
      setActiveSessionLabel(label);
      // Keep the push-suppression signal on the session actually on screen.
      setCurrentSessionId(sessionId);
      // Any in-flight run belongs to the previous session — its frames are
      // now dropped by the session demux, so release the busy gate and start
      // this session's timeline clean (history refetches via the effect).
      activeRunIdRef.current = null;
      committedOffsetRef.current = 0;
      segmentRef.current = 0;
      lastCumulativeLenRef.current = 0;
      cumulativeTextRef.current = null;
      setBusy(false);
      setItems([]);
      setHistoryItems([]);
      setHistoryError(null);
      historyRetryUntilRef.current = null;
      historyRetryCountRef.current = 0;
    },
    [],
  );

  // No cross-agent reset effect here: a different agent is a different
  // COMPONENT INSTANCE (AgentChatRoute keys this screen by agentId), so all
  // conversation state starts clean structurally. The old effect-based reset
  // was also a silent no-op for General→General switches — switchSession's
  // same-session early return — which left the previous agent's transcript
  // on screen until a history refetch happened to replace it.

  const newSession = useCallback(() => {
    // Lowercase RFC-4122 v4 only (contract §1): the server validates, and a
    // mixed-case id would alias-merge two sessions. expo-crypto's randomUUID
    // is already lowercase; toLowerCase is belt-and-braces.
    const id = randomUUID().toLowerCase();
    freshSessionIdsRef.current.add(id);
    switchSession(id, null);
    setSheetOpen(false);
  }, [switchSession]);

  // ---- Roster nav params (contract §1) ----
  // "New task" arrives as `newSession=1` (mint a fresh session + focus the
  // composer); a session picked from the roster's per-agent sheet arrives as
  // `sessionId`(+`sessionLabel`). Both consume their param via router.setParams
  // so the SAME value re-fires on a later navigation — this screen is a
  // persisted tab (href:null), so the param is the only signal a fresh
  // navigation happened, not a remount.
  const startNewSessionFromParam = useCallback(() => {
    newSession();
    inputRef.current?.focus();
  }, [newSession]);
  const clearNewSessionParam = useCallback(
    () => router.setParams({ newSession: undefined }),
    [],
  );
  useNavParamConsumer(
    newSessionParam === "1" ? newSessionParam : undefined,
    startNewSessionFromParam,
    clearNewSessionParam,
  );

  const applySessionFromParam = useCallback(
    (sessionId: string) => switchSession(sessionId, sessionLabelParam ?? null),
    [switchSession, sessionLabelParam],
  );
  const clearSessionParam = useCallback(
    () => router.setParams({ sessionId: undefined, sessionLabel: undefined }),
    [],
  );
  useNavParamConsumer(sessionIdParam, applySessionFromParam, clearSessionParam);

  // ---- Attachment picking ----
  const showSizeError = useCallback((message: string) => {
    if (sizeErrorTimerRef.current) clearTimeout(sizeErrorTimerRef.current);
    setSizeError(message);
    sizeErrorTimerRef.current = setTimeout(() => {
      setSizeError(null);
      sizeErrorTimerRef.current = null;
    }, 5000);
  }, []);

  const dismissSizeError = useCallback(() => {
    if (sizeErrorTimerRef.current) {
      clearTimeout(sizeErrorTimerRef.current);
      sizeErrorTimerRef.current = null;
    }
    setSizeError(null);
  }, []);

  useEffect(
    () => () => {
      if (sizeErrorTimerRef.current) clearTimeout(sizeErrorTimerRef.current);
    },
    [],
  );

  // Oversize gate + pending-list cap, both web-parity (10MB / 10 files).
  const addAttachments = useCallback(
    (candidates: PendingAttachment[]) => {
      const { valid, error } = filterOversized(candidates);
      if (error) showSizeError(error);
      if (valid.length > 0)
        setAttachments((prev) => [...prev, ...valid].slice(0, MAX_ATTACHMENTS));
    },
    [showSizeError],
  );

  const pickFromLibrary = useCallback(async () => {
    const res = await ImagePicker.launchImageLibraryAsync({
      mediaTypes: ["images", "videos"],
      allowsMultipleSelection: true,
      selectionLimit: MAX_ATTACHMENTS,
      quality: 1,
    });
    if (res.canceled) return;
    addAttachments(
      res.assets.map((a) => ({
        id: randomUUID(),
        name: a.fileName ?? a.uri.split("/").pop() ?? "photo",
        size: a.fileSize ?? 0,
        uri: a.uri,
        mimeType: a.mimeType ?? "application/octet-stream",
      })),
    );
  }, [addAttachments]);

  const pickFiles = useCallback(async () => {
    const res = await DocumentPicker.getDocumentAsync({
      multiple: true,
      copyToCacheDirectory: true,
    });
    if (res.canceled) return;
    addAttachments(
      res.assets.map((a) => ({
        id: randomUUID(),
        name: a.name,
        size: a.size ?? 0,
        uri: a.uri,
        mimeType: a.mimeType ?? "application/octet-stream",
      })),
    );
  }, [addAttachments]);

  // "Photo Library" / "Files" / "Cancel" — ActionSheetIOS on iOS (the
  // SessionsSheet delete pattern), Alert elsewhere.
  const openAttachSheet = useCallback(() => {
    const onPickError = (e: unknown) =>
      console.error("attachment pick failed", e);
    if (Platform.OS === "ios") {
      ActionSheetIOS.showActionSheetWithOptions(
        { options: ["Photo Library", "Files", "Cancel"], cancelButtonIndex: 2 },
        (index) => {
          if (index === 0) void pickFromLibrary().catch(onPickError);
          else if (index === 1) void pickFiles().catch(onPickError);
        },
      );
    } else {
      Alert.alert("Attach", undefined, [
        { text: "Photo Library", onPress: () => void pickFromLibrary().catch(onPickError) },
        { text: "Files", onPress: () => void pickFiles().catch(onPickError) },
        { text: "Cancel", style: "cancel" },
      ]);
    }
  }, [pickFromLibrary, pickFiles]);

  const removeAttachment = useCallback((id: string) => {
    setAttachments((prev) => prev.filter((a) => a.id !== id));
  }, []);

  // Full local send pipeline (bubble + busy + dispatch), shared by the
  // composer and the first-run kickoff. Returns false when nothing was sent
  // (empty text, active run, or a dead socket).
  const dispatchText = useCallback(
    (text: string): boolean => {
      if (!text || busy || !agentId) return false;
      const runId = randomUUID();
      try {
        // session_id rides only for a named session — omitted for General so
        // the backend builds the legacy 3-part key (contract §4).
        gateway.sendChat(agentId, text, runId, activeSessionId ?? undefined);
      } catch (e) {
        setItems((prev) => [
          ...prev,
          {
            id: randomUUID(),
            role: "error",
            text: e instanceof Error ? e.message : "Send failed",
          },
        ]);
        return false;
      }
      activeRunIdRef.current = runId;
      committedOffsetRef.current = 0;
      segmentRef.current = 0;
      lastCumulativeLenRef.current = 0;
      cumulativeTextRef.current = null;
      // First send on a minted session materializes it server-side — it now
      // has a transcript, so later reconnect refetches must hit the server.
      if (activeSessionId) freshSessionIdsRef.current.delete(activeSessionId);
      setBusy(true);
      setItems((prev) => [...prev, { id: `u-${runId}`, role: "user", text, runId }]);
      return true;
    },
    [busy, agentId, gateway, activeSessionId],
  );

  // Composer send: attachments upload FIRST (web AgentChatWindow order),
  // then the message goes out with the path notice prepended. One deliberate
  // divergence from web: an upload failure surfaces as an inline error item
  // and sends NOTHING — the draft (text + chips) stays for a retry, instead
  // of sending a message about files the agent can't read.
  const send = useCallback(async () => {
    const text = input.trim();
    if ((!text && attachments.length === 0) || busy || uploading || !agentId)
      return;
    let message = text;
    if (attachments.length > 0) {
      setUploading(true);
      try {
        const uploaded = await uploadAttachments(
          () => getTokenRef.current(),
          agentId,
          attachments,
        );
        message = buildUploadNotice(uploaded) + message;
      } catch (e) {
        setItems((prev) => [
          ...prev,
          {
            id: randomUUID(),
            role: "error",
            text: `File upload failed: ${e instanceof Error ? e.message : "Upload failed"}`,
          },
        ]);
        return;
      } finally {
        setUploading(false);
      }
    }
    if (dispatchText(message)) {
      setInput("");
      setAttachments([]);
    }
  }, [dispatchText, input, attachments, busy, uploading, agentId]);

  // Cancel the in-flight run. agent_abort → upstream chat.abort, targeted at
  // this screen's run. Fire-and-don't-wait-for-terminal (the backend contract):
  // the aborted terminal arrives via the normal stream and releases `busy`
  // there — never here, so a dropped abort can't strand a live run's frames.
  const stopRun = useCallback(() => {
    const runId = activeRunIdRef.current;
    if (!runId || !agentId) return;
    const sent = gateway.send({
      type: "agent_abort",
      agent_id: agentId,
      runId,
      // Same shape rule as sendChat: session_id rides only for a named session.
      ...(activeSessionId ? { session_id: activeSessionId } : {}),
    });
    // The raw send is a silent no-op on a closed socket. The button is gated
    // on isConnected, but that state can trail the socket by a beat — surface
    // the miss instead of swallowing it. The run gate stays up either way;
    // only the run's terminal releases it.
    if (!sent) {
      setItems((prev) => [
        ...prev,
        {
          id: randomUUID(),
          role: "error",
          text: "Connection lost — stop not sent. Try again once reconnected.",
        },
      ]);
    }
  }, [gateway, agentId, activeSessionId]);

  const agentName = name || agentId || "Agent";

  // Seeded history renders first; live frames append after it (contract §2).
  const allItems = useMemo(
    () => (historyItems.length > 0 ? [...historyItems, ...items] : items),
    [historyItems, items],
  );

  // Render-time derivations only — nothing here feeds back into the
  // protocol state above. "Typing" = the run is live but no assistant
  // segment has arrived after the latest user message (spec §7).
  let lastUserIdx = -1;
  for (let i = allItems.length - 1; i >= 0; i--) {
    if (allItems[i].role === "user") {
      lastUserIdx = i;
      break;
    }
  }
  const runHasAssistant = allItems
    .slice(lastUserIdx + 1)
    .some((it) => it.role === "assistant");
  const showTyping = busy && !runHasAssistant;
  // Attachments alone are sendable (web parity: the message becomes just the
  // upload notice).
  const hasDraft = !!input.trim() || attachments.length > 0;
  const lastItem = allItems.length > 0 ? allItems[allItems.length - 1] : undefined;
  // Response groups are anchored at the last user-or-error item; a head
  // rendered past this index belongs to the final group and may pulse.
  let lastAnchorIdx = -1;
  for (let i = allItems.length - 1; i >= 0; i--) {
    if (allItems[i].role === "user" || allItems[i].role === "error") {
      lastAnchorIdx = i;
      break;
    }
  }

  return (
    <KeyboardAvoidingView
      style={styles.container}
      behavior={Platform.OS === "ios" ? "padding" : undefined}
      keyboardVerticalOffset={90}
    >
      <Stack.Screen
        options={{
          // A labeled named session joins the header title (small, ellipsized
          // by the single-line header); General keeps the bare agent name.
          title: activeSessionLabel
            ? `${agentName} — ${activeSessionLabel}`
            : agentName,
          // This screen is a hidden tab sibling (href: null), not a pushed
          // stack screen, so no automatic back chevron — provide one that
          // returns to the agent grid.
          headerLeft: () => (
            <Pressable
              style={styles.headerAction}
              onPress={() => {
                if (router.canGoBack()) router.back();
                else router.replace("/(app)/chat");
              }}
              hitSlop={8}
              accessibilityRole="button"
              accessibilityLabel="Back to agents"
            >
              <Feather name="chevron-left" size={22} color={colors.ink} />
            </Pressable>
          ),
          headerRight: () => (
            <Pressable
              style={styles.headerAction}
              onPress={() => setSheetOpen(true)}
              hitSlop={8}
              accessibilityRole="button"
              accessibilityLabel="Sessions"
            >
              <Feather name="layers" size={18} color={colors.mute} />
            </Pressable>
          ),
        }}
      />
      <FlatList
        ref={listRef}
        testID="chat-timeline"
        data={allItems}
        keyExtractor={(m) => m.id}
        contentContainerStyle={[
          styles.list,
          allItems.length === 0 && !showTyping && !historyLoading && styles.listEmpty,
        ]}
        // Swipe down over the timeline to dismiss the keyboard (iOS tracks the
        // drag); taps on timeline controls land on the control while the
        // keyboard is up instead of only dismissing it.
        keyboardDismissMode="interactive"
        keyboardShouldPersistTaps="handled"
        onContentSizeChange={() => listRef.current?.scrollToEnd({ animated: true })}
        ListHeaderComponent={
          historyError ? (
            // History failure (spec §4 error card): empty timeline + retry;
            // composing stays available.
            <View style={[styles.errorCard, styles.historyErrorCard]}>
              <Text style={styles.errorText}>
                Couldn’t load history — {historyError}
              </Text>
              <Pressable
                onPress={() => {
                  // A deliberate ask opens a fresh silent-retry window.
                  historyRetryUntilRef.current = null;
                  historyRetryCountRef.current = 0;
                  setHistoryNonce((n) => n + 1);
                }}
                accessibilityRole="button"
                accessibilityLabel="Retry loading history"
              >
                <Text style={styles.retryText}>Retry</Text>
              </Pressable>
            </View>
          ) : null
        }
        ListEmptyComponent={
          historyLoading ? (
            <TimelineSkeleton />
          ) : showTyping ? null : (
            <EmptyState agentName={agentName} />
          )
        }
        ListFooterComponent={
          showTyping ? (
            <View
              style={
                lastItem &&
                (lastItem.role === "tool" ? styles.runGap : styles.groupGap)
              }
            >
              <AgentHead name={agentName} active />
            </View>
          ) : null
        }
        renderItem={({ item, index }) => {
          const prev = index > 0 ? allItems[index - 1] : undefined;
          // Assistant segments and tool lines after another assistant/tool
          // item continue the same response group (tight 12px rhythm);
          // everything else starts a new group (40px, spec §1).
          const continuesGroup =
            prev != null && (prev.role === "assistant" || prev.role === "tool");

          if (item.role === "user") {
            return (
              <View style={[styles.userBubble, index > 0 && styles.groupGap]}>
                <Text style={styles.userText}>{item.text}</Text>
              </View>
            );
          }
          if (item.role === "assistant") {
            // The head belongs to the FIRST assistant segment of the group:
            // walk back past interleaved tool lines — hitting a user/error
            // anchor (or the list start) means this segment opens the group;
            // hitting another assistant segment means it continues one.
            let showHead = true;
            for (let i = index - 1; i >= 0; i--) {
              const role = allItems[i].role;
              if (role === "assistant") {
                showHead = false;
                break;
              }
              if (role === "user" || role === "error") break;
            }
            return (
              <View
                style={[
                  styles.assistantGroup,
                  index > 0 && (continuesGroup ? styles.runGap : styles.groupGap),
                ]}
              >
                {showHead && (
                  <AgentHead
                    name={agentName}
                    active={busy && index > lastAnchorIdx}
                  />
                )}
                <MessageMarkdown text={item.text} />
              </View>
            );
          }
          if (item.role === "tool") {
            // Render-time settle: a pill whose tool_end never matched (e.g.
            // missing toolCallId) must not pulse forever — once the run is
            // over it reads as done.
            const rawStatus = item.toolStatus ?? "running";
            const pillStatus: ToolPillStatus =
              rawStatus === "running" && !busy ? "done" : rawStatus;
            // Gap rhythm (spec §3): consecutive pills cluster at 8px; a pill
            // opening a response group — directly after a user bubble OR an
            // error card — takes the 40px group gap; a pill continuing an
            // assistant run takes the 12px run gap.
            return (
              <View
                style={
                  index > 0
                    ? prev?.role === "tool"
                      ? styles.pillGap
                      : prev?.role === "assistant"
                        ? styles.runGap
                        : styles.groupGap
                    : undefined
                }
              >
                <ToolPill
                  tool={item.tool ?? item.text}
                  status={pillStatus}
                  args={item.args}
                  result={item.result}
                />
              </View>
            );
          }
          // Error frame: full-width card, not a bubble (spec §4). Coded
          // free-tier gate errors render the structured upgrade card.
          return (
            <View style={index > 0 ? styles.groupGap : undefined}>
              <ChatErrorCard code={item.code} message={item.text} />
            </View>
          );
        }}
      />
      {/* Composer (spec §4). While a run is active the send button becomes a
          Stop button (agent_abort → upstream chat.abort). The attach button
          feeds the pending-chip row above the input; files upload on send. */}
      <View style={styles.composerOuter}>
        {!gateway.isConnected && <ReconnectingBanner />}
        {sizeError && (
          <View style={styles.sizeErrorRow}>
            <Text style={styles.sizeErrorText}>{sizeError}</Text>
            <Pressable
              onPress={dismissSizeError}
              hitSlop={8}
              accessibilityRole="button"
              accessibilityLabel="Dismiss size error"
            >
              <Feather name="x" size={12} color={colors.errorText} />
            </Pressable>
          </View>
        )}
        <View style={[styles.card, inputFocused && styles.cardFocused]}>
          <AttachmentChips attachments={attachments} onRemove={removeAttachment} />
          <View style={styles.inputZone}>
            <TextInput
              ref={inputRef}
              style={styles.input}
              placeholder="Ask anything"
              placeholderTextColor={colors.placeholder}
              value={input}
              onChangeText={setInput}
              multiline
              editable={gateway.isConnected && !uploading}
              onFocus={() => setInputFocused(true)}
              onBlur={() => setInputFocused(false)}
            />
          </View>
          <View style={styles.rail}>
            <Pressable
              style={styles.attachButton}
              onPress={openAttachSheet}
              disabled={uploading}
              hitSlop={4}
              accessibilityRole="button"
              accessibilityLabel="Attach"
            >
              <Feather
                name="plus"
                size={18}
                color={uploading ? colors.placeholder : colors.mute}
              />
            </Pressable>
            {busy ? (
              // Gated on the socket like the TextInput above: a dead socket
              // makes stopRun's raw send a silent no-op, so an active-looking
              // Stop would eat taps without feedback.
              <Pressable
                style={[
                  styles.sendButton,
                  !gateway.isConnected && styles.sendDisabled,
                ]}
                onPress={stopRun}
                disabled={!gateway.isConnected}
                accessibilityRole="button"
                accessibilityLabel="Stop"
              >
                <Feather
                  name="square"
                  size={14}
                  color={!gateway.isConnected ? colors.placeholder : colors.white}
                />
              </Pressable>
            ) : (
              <Pressable
                style={[styles.sendButton, !hasDraft && styles.sendDisabled]}
                onPress={() => void send()}
                disabled={!hasDraft || uploading}
                accessibilityRole="button"
                accessibilityLabel="Send"
              >
                {uploading ? (
                  // Upload in flight: keep the filled button (the draft is
                  // still staged) but gate presses so a double-tap can't
                  // double-upload — web's Loader2-on-isUploading equivalent.
                  <ActivityIndicator size="small" color={colors.white} />
                ) : (
                  <Feather
                    name="arrow-up"
                    size={16}
                    color={!hasDraft ? colors.placeholder : colors.white}
                  />
                )}
              </Pressable>
            )}
          </View>
        </View>
      </View>
      {agentId ? (
        <SessionsSheet
          visible={sheetOpen}
          onClose={() => setSheetOpen(false)}
          agentId={agentId}
          userId={userId}
          activeSessionId={activeSessionId}
          onSelect={switchSession}
          onNewSession={newSession}
        />
      ) : null}
    </KeyboardAvoidingView>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: colors.cream },
  // Spec §1 verbatim: 16px horizontal padding, 32px (py-8) top/bottom.
  list: {
    paddingHorizontal: spacing.lg,
    paddingTop: 32,
    paddingBottom: 32,
  },
  listEmpty: { flexGrow: 1 },
  // 40px between message groups (space-y-10), 12px within a response run,
  // 8px between consecutive tool pills in a cluster (spec §3 gap-8).
  groupGap: { marginTop: 40 },
  runGap: { marginTop: spacing.md },
  pillGap: { marginTop: spacing.sm },

  // USER (spec §1): right-aligned cream bubble, ink text, squared-off
  // bottom-right corner.
  userBubble: {
    alignSelf: "flex-end",
    maxWidth: "85%",
    backgroundColor: colors.userBubble,
    borderRadius: radii.xl,
    borderBottomRightRadius: radii.sm,
    paddingHorizontal: spacing.lg,
    paddingVertical: 10,
  },
  userText: {
    fontFamily: fonts.dmSans,
    fontSize: 14,
    lineHeight: 28,
    color: colors.ink,
  },

  // ASSISTANT (spec §1): flat on the page — no bubble.
  assistantGroup: { alignSelf: "flex-start", maxWidth: "85%" },

  // Error card (spec §4): full-width tinted frame.
  errorCard: {
    backgroundColor: colors.errorBg,
    borderWidth: 1,
    borderColor: colors.errorBorder,
    borderRadius: radii.md,
    padding: spacing.lg,
  },
  errorText: {
    fontFamily: fonts.dmSans,
    fontSize: 14,
    lineHeight: 20,
    color: colors.errorText,
  },
  // History-load failure variant: same §4 card, plus the retry affordance
  // and a bottom gap before the (empty) timeline.
  historyErrorCard: { marginBottom: spacing.lg, gap: spacing.sm },
  retryText: {
    fontFamily: fonts.dmSansMedium,
    fontSize: 13,
    lineHeight: 18,
    color: colors.forest,
  },

  // Header-right sessions affordance.
  headerAction: {
    paddingHorizontal: spacing.lg,
    paddingVertical: spacing.sm,
  },

  // Composer (spec §4): floating white card on the cream page.
  composerOuter: {
    paddingHorizontal: spacing.lg,
    paddingBottom: spacing.lg,
    backgroundColor: colors.cream,
  },
  // Web shadow `0 1px 2px rgba(26,26,26,.03), 0 4px 16px rgba(26,26,26,.05)`
  // — RN takes one shadow, so the softer/larger layer wins (+ elevation).
  card: {
    backgroundColor: colors.white,
    borderRadius: radii.xl,
    borderWidth: 1,
    borderColor: colors.edge,
    shadowColor: colors.ink,
    shadowOffset: { width: 0, height: 4 },
    shadowOpacity: 0.05,
    shadowRadius: 16,
    elevation: 3,
  },
  // Focused: forest border + slightly stronger shadow (web: 0 6px 24px .08).
  cardFocused: {
    borderColor: colors.forestFocusBorder,
    shadowOffset: { width: 0, height: 6 },
    shadowOpacity: 0.08,
    shadowRadius: 24,
    elevation: 4,
  },
  inputZone: { paddingHorizontal: spacing.lg, paddingTop: spacing.md },
  // Native multiline auto-grow: no fixed height (a controlled height doesn't
  // track typing reliably on Fabric iOS), capped at 200. Zero the
  // platform-default vertical padding so the box hugs the text.
  input: {
    fontFamily: fonts.dmSans,
    fontSize: 15,
    lineHeight: 24,
    maxHeight: 200,
    color: colors.ink,
    paddingTop: 0,
    paddingBottom: 0,
    textAlignVertical: "top",
  },
  rail: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.xs,
    paddingHorizontal: spacing.sm,
    paddingBottom: spacing.sm,
    paddingTop: 6,
  },
  // Ghost icon button (web's Plus attach affordance) on the rail's left edge.
  attachButton: {
    width: 32,
    height: 32,
    borderRadius: 16,
    alignItems: "center",
    justifyContent: "center",
  },
  sendButton: {
    marginLeft: "auto",
    width: 32,
    height: 32,
    borderRadius: 16,
    backgroundColor: colors.forest,
    alignItems: "center",
    justifyContent: "center",
  },
  sendDisabled: { backgroundColor: colors.creamPressed },

  // Oversize-rejection banner (web sizeError row): danger-tinted, dismissible.
  sizeErrorRow: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.sm,
    backgroundColor: colors.errorBg,
    borderWidth: 1,
    borderColor: colors.errorBorder,
    borderRadius: radii.md,
    paddingHorizontal: spacing.md,
    paddingVertical: 6,
    marginBottom: spacing.sm,
  },
  sizeErrorText: {
    flex: 1,
    fontFamily: fonts.dmSans,
    fontSize: 12,
    lineHeight: 16,
    color: colors.errorText,
  },

  // Disconnected banner (spec §4).
  banner: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.sm,
    backgroundColor: colors.bannerBg,
    borderWidth: 1,
    borderColor: colors.bannerBorder,
    borderRadius: radii.md,
    paddingHorizontal: spacing.md,
    paddingVertical: 6,
    marginBottom: spacing.sm,
  },
  bannerDot: {
    width: 6,
    height: 6,
    borderRadius: 3,
    backgroundColor: colors.connectingYellow,
  },
  bannerText: {
    fontFamily: fonts.dmSansMedium,
    fontSize: 12,
    lineHeight: 16,
    color: colors.bannerText,
  },
});
