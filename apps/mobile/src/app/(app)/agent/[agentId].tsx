import Feather from "@expo/vector-icons/Feather";
import { randomUUID } from "expo-crypto";
import { Stack, useLocalSearchParams } from "expo-router";
import { useCallback, useEffect, useRef, useState } from "react";
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
import { EmptyState } from "../../../components/chat/EmptyState";
import { MessageMarkdown } from "../../../components/chat/MessageMarkdown";
import { ToolPill, type ToolPillStatus } from "../../../components/chat/ToolPill";
import { usePulse } from "../../../components/chat/usePulse";
import { colors, fonts, radii, spacing } from "../../../config/theme";
import { useGateway } from "../../../providers/gateway";

interface ChatItem {
  id: string;
  role: "user" | "assistant" | "tool" | "error";
  text: string;
  streaming?: boolean;
  /** Tool items only: tool name + input args from the tool_start frame. */
  tool?: string;
  args?: Record<string, unknown>;
  /** Tool items only: result text attached when tool_end/tool_error lands. */
  result?: string;
  toolStatus?: ToolPillStatus;
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
 * Phase 2 MVP chat: streams one conversation with one agent over the shared
 * gateway. Transcript is in-memory only (history/sessions land later, per
 * plan Phase 2 Task 5); protocol handling follows the web contract:
 *
 * - `chunk.content` is CUMULATIVE — the full assistant text from the start
 *   of the run on every frame, not a delta (see backend connection_pool
 *   "cumulative replacement" and web useAgentChat's segment model). Bubbles
 *   REPLACE their text, and a per-run committed offset splits the cumulative
 *   stream into per-segment bubbles when tool calls interleave.
 * - `run_ended` (not `done`) releases the busy gate — `done` also fires on
 *   interim tool-approval brackets.
 *
 * Presentation follows the web design spec §1/§2/§7: user text in a cream
 * bubble on the right, assistant markdown flat on the page under an
 * AgentHead whose glyph pulses while the run is active.
 */
export default function AgentChat() {
  const { agentId, name } = useLocalSearchParams<{ agentId: string; name?: string }>();
  const gateway = useGateway();
  const [items, setItems] = useState<ChatItem[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  // Presentational-only composer state (spec §4): focus ring + auto-grow.
  const [inputFocused, setInputFocused] = useState(false);
  const [inputHeight, setInputHeight] = useState(24);
  const activeRunIdRef = useRef<string | null>(null);
  // Cumulative-stream bookkeeping for the active run: how many chars of the
  // cumulative text earlier segments already display, the current segment
  // ordinal (keys stay unique when tools split the stream), and the last
  // cumulative length seen (becomes the offset when a tool interrupts).
  const committedOffsetRef = useRef(0);
  const segmentRef = useRef(0);
  const lastCumulativeLenRef = useRef(0);
  const listRef = useRef<FlatList<ChatItem>>(null);

  useEffect(() => {
    return gateway.onChatMessage((msg) => {
      if (!("agent_id" in msg) || msg.agent_id !== agentId) return;
      const runId = "runId" in msg ? msg.runId : undefined;
      // Only render frames for the run this screen started — background runs
      // (cron, channels) for the same agent stay out of this transcript.
      if (runId && runId !== activeRunIdRef.current) return;

      if (msg.type === "chunk") {
        const segmentId = `${runId}-s${segmentRef.current}`;
        const segmentText = msg.content.slice(committedOffsetRef.current);
        lastCumulativeLenRef.current = msg.content.length;
        setItems((prev) => {
          const idx = prev.findIndex((it) => it.id === segmentId);
          if (idx >= 0) {
            const next = [...prev];
            next[idx] = { ...next[idx], text: segmentText };
            return next;
          }
          return [...prev, { id: segmentId, role: "assistant", text: segmentText, streaming: true }];
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
          { id: randomUUID(), role: "error", text: msg.message },
        ]);
      } else if (msg.type === "run_ended") {
        setItems((prev) =>
          prev.map((it) => (it.streaming ? { ...it, streaming: false } : it)),
        );
        activeRunIdRef.current = null;
        setBusy(false);
      }
    });
  }, [gateway, agentId]);

  const send = useCallback(() => {
    const text = input.trim();
    if (!text || busy || !agentId) return;
    const runId = randomUUID();
    try {
      gateway.sendChat(agentId, text, runId);
    } catch (e) {
      setItems((prev) => [
        ...prev,
        {
          id: randomUUID(),
          role: "error",
          text: e instanceof Error ? e.message : "Send failed",
        },
      ]);
      return;
    }
    activeRunIdRef.current = runId;
    committedOffsetRef.current = 0;
    segmentRef.current = 0;
    lastCumulativeLenRef.current = 0;
    setBusy(true);
    setInput("");
    setItems((prev) => [...prev, { id: `u-${runId}`, role: "user", text }]);
  }, [input, busy, agentId, gateway]);

  const agentName = name || agentId || "Agent";

  // Render-time derivations only — nothing here feeds back into the
  // protocol state above. "Typing" = the run is live but no assistant
  // segment has arrived after the latest user message (spec §7).
  let lastUserIdx = -1;
  for (let i = items.length - 1; i >= 0; i--) {
    if (items[i].role === "user") {
      lastUserIdx = i;
      break;
    }
  }
  const runHasAssistant = items
    .slice(lastUserIdx + 1)
    .some((it) => it.role === "assistant");
  const showTyping = busy && !runHasAssistant;
  const lastItem = items.length > 0 ? items[items.length - 1] : undefined;
  // Response groups are anchored at the last user-or-error item; a head
  // rendered past this index belongs to the final group and may pulse.
  let lastAnchorIdx = -1;
  for (let i = items.length - 1; i >= 0; i--) {
    if (items[i].role === "user" || items[i].role === "error") {
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
      <Stack.Screen options={{ title: agentName }} />
      <FlatList
        ref={listRef}
        data={items}
        keyExtractor={(m) => m.id}
        contentContainerStyle={[
          styles.list,
          items.length === 0 && !showTyping && styles.listEmpty,
        ]}
        onContentSizeChange={() => listRef.current?.scrollToEnd({ animated: true })}
        ListEmptyComponent={
          showTyping ? null : <EmptyState agentName={agentName} />
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
          const prev = index > 0 ? items[index - 1] : undefined;
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
              const role = items[i].role;
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
          // Error frame: full-width card, not a bubble (spec §4).
          return (
            <View style={[styles.errorCard, index > 0 && styles.groupGap]}>
              <Text style={styles.errorText}>{item.text}</Text>
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
    borderColor: colors.forest,
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
