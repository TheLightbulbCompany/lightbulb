import { useUser } from "@clerk/clerk-expo";
import Feather from "@expo/vector-icons/Feather";
import {
  accumulateStreamText,
  useGatewayRpc,
  type AgentsListResponse,
} from "@isol8/shared";
import { randomUUID } from "expo-crypto";
import { router, Stack, useFocusEffect, useLocalSearchParams } from "expo-router";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
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
import { ChatErrorCard } from "../../../components/chat/ChatErrorCard";
import type { ChatItem } from "../../../components/chat/chatItems";
import { EmptyState } from "../../../components/chat/EmptyState";
import { MessageMarkdown } from "../../../components/chat/MessageMarkdown";
import { SessionsSheet } from "../../../components/chat/SessionsSheet";
import { TimelineSkeleton } from "../../../components/chat/TimelineSkeleton";
import { ToolPill, type ToolPillStatus } from "../../../components/chat/ToolPill";
import { usePulse } from "../../../components/chat/usePulse";
import { useFirstRunKickoff } from "../../../components/onboarding/useFirstRunKickoff";
import { colors, fonts, radii, spacing } from "../../../config/theme";
import { setCurrentAgentId } from "../../../lib/activeAgent";
import { projectHistoryToChatItems } from "../../../lib/chatHistory";
import { readFirstRun, writeFirstRun } from "../../../lib/firstRun";
import { buildChatSessionKey } from "../../../lib/sessionKey";
import { useGateway } from "../../../providers/gateway";

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
 * - `chunk` frames arrive as append deltas (`append: true`) or full-text
 *   resyncs; accumulateStreamText reconstructs the run's CUMULATIVE text
 *   (matching the web useAgentChat segment model). Bubbles REPLACE their
 *   text from that buffer, and a per-run committed offset splits the
 *   cumulative stream into per-segment bubbles when tool calls interleave.
 * - `run_ended` (not `done`) releases the busy gate — `done` also fires on
 *   interim tool-approval brackets.
 *
 * Presentation follows the web design spec §1/§2/§7: user text in a cream
 * bubble on the right, assistant markdown flat on the page under an
 * AgentHead whose glyph pulses while the run is active.
 */
export default function AgentChat() {
  const { agentId, name, newSession: newSessionParam } =
    useLocalSearchParams<{ agentId: string; name?: string; newSession?: string }>();
  // Publish the focused agent so the push notification handler suppresses the
  // banner for a reply the user is actively watching stream in (activeAgent.ts).
  useFocusEffect(
    useCallback(() => {
      setCurrentAgentId(agentId ?? null);
      return () => setCurrentAgentId(null);
    }, [agentId]),
  );
  const gateway = useGateway();
  // Session keys embed the Clerk user id (contract §1); history/session
  // fetches wait for Clerk to load it.
  const { user } = useUser();
  const userId = user?.id ?? null;
  // Ref twin for the frame handler's run_ended branch — the subscription
  // effect's deps stay [gateway, agentId], so it must read the CURRENT user
  // (Clerk hands out a new object per render) to see fresh firstRun flags.
  const userRef = useRef(user);
  userRef.current = user;
  const [items, setItems] = useState<ChatItem[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  // Presentational-only composer state (spec §4): focus ring + auto-grow.
  const [inputFocused, setInputFocused] = useState(false);
  const [inputHeight, setInputHeight] = useState(24);
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
  const activeRunIdRef = useRef<string | null>(null);
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
      const runId = "runId" in msg ? msg.runId : undefined;
      // Only render frames for the run this screen started — background runs
      // (cron, channels) for the same agent stay out of this transcript.
      if (runId && runId !== activeRunIdRef.current) return;

      if (msg.type === "chunk") {
        const segmentId = `${runId}-s${segmentRef.current}`;
        const cumulative = accumulateStreamText(cumulativeTextRef.current, msg);
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
        setItems((prev) => [
          ...prev,
          { id: randomUUID(), role: "error", text: msg.message, code: msg.code },
        ]);
        // Additive gate release: gate-level errors are terminal but never
        // followed by a run_ended, so the error frame itself must release
        // the run gate — otherwise `busy` wedges true forever. Redundant
        // (but harmless) when a run_ended does follow.
        activeRunIdRef.current = null;
        setBusy(false);
      } else if (msg.type === "run_ended") {
        setItems((prev) =>
          prev.map((it) => (it.streaming ? { ...it, streaming: false } : it)),
        );
        activeRunIdRef.current = null;
        setBusy(false);
        // First-run show completion (web AgentChatWindow parity): `started`
        // is only ever true when the kickoff hook fired it, so any completed
        // run here closes out the first-run show.
        const currentUser = userRef.current;
        if (currentUser) {
          const fr = readFirstRun(currentUser);
          if (fr.started && !fr.completed) {
            void writeFirstRun(currentUser, { completed: true }).catch((err) =>
              console.error("first-run completion write failed", err),
            );
          }
        }
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
    if (!agentId || !userId || !isConnected) return;
    if (activeSessionId && freshSessionIdsRef.current.has(activeSessionId)) {
      // Brand-new locally-minted session: no server transcript exists until
      // the first send, so there is nothing to fetch (contract §1).
      setHistoryItems([]);
      setHistoryLoading(false);
      setHistoryError(null);
      return;
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
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        // Not latched: the effect re-fires on the next reconnect / session
        // visit, and the error card's Retry bumps historyNonce.
        setHistoryLoading(false);
        setHistoryError(
          err instanceof Error ? err.message : "Failed to load history",
        );
      });
    return () => {
      cancelled = true;
    };
  }, [agentId, userId, activeSessionId, isConnected, sendReq, historyNonce]);

  // ---- Session switching ----
  const switchSession = useCallback(
    (sessionId: string | null, label: string | null) => {
      if (sessionId === activeSessionIdRef.current) return;
      activeSessionIdRef.current = sessionId;
      setActiveSessionId(sessionId);
      setActiveSessionLabel(label);
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
    },
    [],
  );

  // A different agent means a different session namespace — never carry a
  // session id across agents (the route component is reused across params).
  const prevAgentIdRef = useRef(agentId);
  useEffect(() => {
    if (prevAgentIdRef.current === agentId) return;
    prevAgentIdRef.current = agentId;
    switchSession(null, null);
  }, [agentId, switchSession]);

  const newSession = useCallback(() => {
    // Lowercase RFC-4122 v4 only (contract §1): the server validates, and a
    // mixed-case id would alias-merge two sessions. expo-crypto's randomUUID
    // is already lowercase; toLowerCase is belt-and-braces.
    const id = randomUUID().toLowerCase();
    freshSessionIdsRef.current.add(id);
    switchSession(id, null);
    setSheetOpen(false);
  }, [switchSession]);

  // "New task" from the roster navigates here with `newSession=1`: mint a
  // fresh session once on mount and focus the composer. A ref guard keeps it
  // from re-firing on re-render (the param stays in the URL for the screen's
  // lifetime).
  const didNewSessionParamRef = useRef(false);
  useEffect(() => {
    if (newSessionParam === "1" && !didNewSessionParamRef.current) {
      didNewSessionParamRef.current = true;
      newSession();
      inputRef.current?.focus();
    }
  }, [newSessionParam, newSession]);

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

  const send = useCallback(() => {
    if (dispatchText(input.trim())) setInput("");
  }, [dispatchText, input]);

  // First-run kickoff (web useFirstRunKickoff parity): once the funnel brief
  // is submitted and this screen shows the DEFAULT agent (Friday) over a live
  // socket, auto-send the visible kickoff prompt exactly once. agents.list is
  // SWR-cached, so this shares the roster screen's fetch.
  const { data: rosterData } = useGatewayRpc<AgentsListResponse>(
    gateway,
    "agents.list",
  );
  useFirstRunKickoff({
    user,
    agentId: agentId ?? null,
    defaultId: rosterData?.defaultId ?? null,
    isConnected: gateway.isConnected,
    send: dispatchText,
  });

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
        data={allItems}
        keyExtractor={(m) => m.id}
        contentContainerStyle={[
          styles.list,
          allItems.length === 0 && !showTyping && !historyLoading && styles.listEmpty,
        ]}
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
                onPress={() => setHistoryNonce((n) => n + 1)}
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
      {/* Composer (spec §4). Same logic as before — identical disabled
          conditions and send semantics; only the presentation changed. No
          stop button v1: the screen has no abort path (the mobile gateway
          exposes no chat.abort helper yet). Attach is non-functional v1 —
          no upload flow on mobile. */}
      <View style={styles.composerOuter}>
        {!gateway.isConnected && <ReconnectingBanner />}
        <View style={[styles.card, inputFocused && styles.cardFocused]}>
          <View style={styles.inputZone}>
            <TextInput
              ref={inputRef}
              style={[styles.input, { height: inputHeight }]}
              placeholder="Ask anything"
              placeholderTextColor={colors.placeholder}
              value={input}
              onChangeText={setInput}
              multiline
              editable={gateway.isConnected}
              onFocus={() => setInputFocused(true)}
              onBlur={() => setInputFocused(false)}
              onContentSizeChange={(e) =>
                setInputHeight(
                  Math.min(200, Math.max(24, e.nativeEvent.contentSize.height)),
                )
              }
            />
          </View>
          <View style={styles.rail}>
            <View style={styles.attachButton}>
              <Feather name="plus" size={16} color={colors.mute} />
            </View>
            <Pressable
              style={[
                styles.sendButton,
                (busy || !input.trim()) && styles.sendDisabled,
              ]}
              onPress={send}
              disabled={busy || !input.trim()}
              accessibilityRole="button"
              accessibilityLabel="Send"
            >
              <Feather
                name="arrow-up"
                size={16}
                color={busy || !input.trim() ? colors.placeholder : colors.white}
              />
            </Pressable>
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
  // Height is driven by onContentSizeChange (auto-grow 24–200); zero the
  // platform-default vertical padding so the measured height is the text's.
  input: {
    fontFamily: fonts.dmSans,
    fontSize: 15,
    lineHeight: 24,
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
  // Attach affordance — disabled/non-functional v1, rendered at 40%.
  attachButton: {
    width: 32,
    height: 32,
    borderRadius: 16,
    alignItems: "center",
    justifyContent: "center",
    opacity: 0.4,
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
