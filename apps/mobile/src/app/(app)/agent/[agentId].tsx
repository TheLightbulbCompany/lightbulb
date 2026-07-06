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

import { colors } from "../../../config/theme";
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

  return (
    <KeyboardAvoidingView
      style={styles.container}
      behavior={Platform.OS === "ios" ? "padding" : undefined}
      keyboardVerticalOffset={90}
    >
      <Stack.Screen options={{ title: name || agentId }} />
      <FlatList
        ref={listRef}
        data={items}
        keyExtractor={(m) => m.id}
        contentContainerStyle={styles.list}
        onContentSizeChange={() => listRef.current?.scrollToEnd({ animated: true })}
        renderItem={({ item }) => (
          <View
            style={[
              styles.bubble,
              item.role === "user" && styles.userBubble,
              item.role === "tool" && styles.toolLine,
              item.role === "error" && styles.errorBubble,
            ]}
          >
            <Text
              style={[
                styles.bubbleText,
                item.role === "user" && styles.userText,
                item.role === "tool" && styles.toolText,
                item.role === "error" && styles.errorText,
              ]}
            >
              {item.text}
              {item.streaming ? " ▍" : ""}
            </Text>
          </View>
        )}
      />
      <View style={styles.composer}>
        <TextInput
          style={styles.input}
          placeholder={gateway.isConnected ? "Message" : "Reconnecting…"}
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
  container: { flex: 1 },
  list: { padding: 12, gap: 8 },
  bubble: {
    alignSelf: "flex-start",
    maxWidth: "85%",
    backgroundColor: colors.creamTertiary,
    borderRadius: 14,
    paddingHorizontal: 14,
    paddingVertical: 10,
  },
  userBubble: { alignSelf: "flex-end", backgroundColor: colors.ink },
  toolLine: { backgroundColor: "transparent", paddingVertical: 2 },
  errorBubble: { backgroundColor: colors.creamTertiary },
  bubbleText: { fontSize: 15, lineHeight: 21 },
  userText: { color: colors.white },
  toolText: { fontSize: 12, opacity: 0.5 },
  errorText: { color: colors.error },
  composer: {
    flexDirection: "row",
    alignItems: "flex-end",
    gap: 8,
    padding: 10,
    borderTopWidth: StyleSheet.hairlineWidth,
    borderTopColor: colors.border,
  },
  input: {
    flex: 1,
    maxHeight: 120,
    borderWidth: 1,
    borderColor: colors.border,
    borderRadius: 18,
    paddingHorizontal: 14,
    paddingVertical: 9,
    fontSize: 15,
  },
  sendButton: {
    backgroundColor: colors.ink,
    borderRadius: 18,
    paddingHorizontal: 16,
    paddingVertical: 10,
  },
  sendDisabled: { opacity: 0.4 },
  sendText: { color: colors.white, fontSize: 14, fontWeight: "600" },
});
