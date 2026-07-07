import { randomUUID } from "expo-crypto";
import { Stack, useLocalSearchParams } from "expo-router";
import { useCallback, useEffect, useRef, useState } from "react";
import {
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
import { colors, fonts, radii, spacing, type } from "../../../config/theme";
import { useGateway } from "../../../providers/gateway";

interface ChatItem {
  id: string;
  role: "user" | "assistant" | "tool" | "error";
  text: string;
  streaming?: boolean;
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
          { id: `${msg.toolCallId ?? randomUUID()}`, role: "tool", text: `⚙ ${msg.tool}` },
        ]);
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
            return (
              <View
                style={[
                  styles.assistantGroup,
                  index > 0 && (continuesGroup ? styles.runGap : styles.groupGap),
                ]}
              >
                {!continuesGroup && (
                  <AgentHead
                    name={agentName}
                    active={busy && index > lastUserIdx}
                  />
                )}
                <MessageMarkdown text={item.text} />
              </View>
            );
          }
          if (item.role === "tool") {
            return (
              <View style={[styles.toolLine, index > 0 && styles.runGap]}>
                <Text style={styles.toolText}>{item.text}</Text>
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
      <View style={styles.composer}>
        <TextInput
          style={styles.input}
          placeholder={gateway.isConnected ? "Message" : "Reconnecting…"}
          placeholderTextColor={colors.muted}
          value={input}
          onChangeText={setInput}
          multiline
          editable={gateway.isConnected}
        />
        <Pressable
          style={[styles.sendButton, (busy || !input.trim()) && styles.sendDisabled]}
          onPress={send}
          disabled={busy || !input.trim()}
        >
          <Text style={styles.sendText}>{busy ? "…" : "Send"}</Text>
        </Pressable>
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
  // 40px between message groups (space-y-10), 12px within a response run.
  groupGap: { marginTop: 40 },
  runGap: { marginTop: spacing.md },

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

  // Tool line — placeholder visuals until the P3 tool-pill pass.
  toolLine: { alignSelf: "flex-start", paddingVertical: spacing.xs },
  toolText: { ...type.caption },

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

  // Composer — logic and visuals unchanged (P3 restyles this).
  composer: {
    flexDirection: "row",
    alignItems: "flex-end",
    gap: spacing.sm,
    padding: spacing.md,
    backgroundColor: colors.cream,
    borderTopWidth: StyleSheet.hairlineWidth,
    borderTopColor: colors.border,
  },
  input: {
    flex: 1,
    maxHeight: 120,
    backgroundColor: colors.white,
    borderWidth: 1,
    borderColor: colors.border,
    borderRadius: radii.lg,
    paddingHorizontal: spacing.md,
    paddingVertical: spacing.sm,
    fontFamily: fonts.dmSans,
    fontSize: 15,
    color: colors.ink,
  },
  sendButton: {
    backgroundColor: colors.forest,
    borderRadius: radii.lg,
    paddingHorizontal: spacing.lg,
    paddingVertical: spacing.sm,
  },
  sendDisabled: { opacity: 0.4 },
  sendText: {
    color: colors.white,
    fontFamily: fonts.dmSansMedium,
    fontSize: 15,
    lineHeight: 22,
  },
});
