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
 * plan Phase 2 Task 5); protocol handling follows the web contract — chunks
 * accumulate per runId, `run_ended` (not `done`) releases the busy gate.
 */
export default function AgentChat() {
  const { agentId, name } = useLocalSearchParams<{ agentId: string; name?: string }>();
  const gateway = useGateway();
  const [items, setItems] = useState<ChatItem[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const activeRunIdRef = useRef<string | null>(null);
  const listRef = useRef<FlatList<ChatItem>>(null);

  useEffect(() => {
    return gateway.onChatMessage((msg) => {
      if (!("agent_id" in msg) || msg.agent_id !== agentId) return;
      const runId = "runId" in msg ? msg.runId : undefined;
      // Only render frames for the run this screen started — background runs
      // (cron, channels) for the same agent stay out of this transcript.
      if (runId && runId !== activeRunIdRef.current) return;

      if (msg.type === "chunk") {
        setItems((prev) => {
          const last = prev[prev.length - 1];
          if (last?.streaming && last.role === "assistant" && last.id === runId) {
            return [...prev.slice(0, -1), { ...last, text: last.text + msg.content }];
          }
          return [...prev, { id: runId!, role: "assistant", text: msg.content, streaming: true }];
        });
      } else if (msg.type === "tool_start") {
        setItems((prev) => [
          ...prev,
          { id: `${msg.toolCallId ?? randomUUID()}`, role: "tool", text: `⚙ ${msg.tool}` },
        ]);
      } else if (msg.type === "error") {
        setItems((prev) => [
          ...prev,
          { id: randomUUID(), role: "error", text: msg.message },
        ]);
      } else if (msg.type === "run_ended") {
        // Authoritative end-of-run — releases the composer regardless of
        // outcome (done/error/aborted). `done` alone is NOT sufficient (it
        // also fires on interim tool-approval brackets).
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
    backgroundColor: "#f0ece2",
    borderRadius: 14,
    paddingHorizontal: 14,
    paddingVertical: 10,
  },
  userBubble: { alignSelf: "flex-end", backgroundColor: "#1a1a1a" },
  toolLine: { backgroundColor: "transparent", paddingVertical: 2 },
  errorBubble: { backgroundColor: "#fbe9e7" },
  bubbleText: { fontSize: 15, lineHeight: 21 },
  userText: { color: "#fff" },
  toolText: { fontSize: 12, opacity: 0.5 },
  errorText: { color: "#b3261e" },
  composer: {
    flexDirection: "row",
    alignItems: "flex-end",
    gap: 8,
    padding: 10,
    borderTopWidth: StyleSheet.hairlineWidth,
    borderTopColor: "#d5d0c4",
  },
  input: {
    flex: 1,
    maxHeight: 120,
    borderWidth: 1,
    borderColor: "#d5d0c4",
    borderRadius: 18,
    paddingHorizontal: 14,
    paddingVertical: 9,
    fontSize: 15,
  },
  sendButton: {
    backgroundColor: "#1a1a1a",
    borderRadius: 18,
    paddingHorizontal: 16,
    paddingVertical: 10,
  },
  sendDisabled: { opacity: 0.4 },
  sendText: { color: "#fff", fontSize: 14, fontWeight: "600" },
});
