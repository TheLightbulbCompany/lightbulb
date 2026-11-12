// A plain text chat with the assistant (web's components/voice/AssistantThread): its messages, a composer and a
// "replying" line, over the same rooms hook and live room subscription. No threads, task cards, details or
// onboarding: talking is the orb. Any other room (a call or a meeting) opens here as a read-only record.
import { randomUUID } from "expo-crypto";
import { Stack, useFocusEffect, useLocalSearchParams } from "expo-router";
import { useCallback, useEffect, useRef, useState } from "react";
import { FlatList, Pressable, StyleSheet, Text, TextInput, View } from "react-native";
import Markdown from "react-native-markdown-display";
import { KeyboardAvoidingView } from "react-native-keyboard-controller";
import { useSafeAreaInsets } from "react-native-safe-area-context";

import { fonts, radii, spacing, useChatPalette } from "../../../../config/theme";
import { usePushedScreenKeyboardOffset } from "../../../../lib/keyboardOffset";
import { useMobileRooms } from "../../../../lib/rooms";
import { isTextChat } from "../../../../lib/textChat";
import type { CanonicalRoomMessage } from "@lightbulb/shared";

type Row = { key: string; human: boolean; name: string; text: string; pending?: boolean; unsent?: boolean };

/** What a line says. A line with no text (an attachment, a task card) says what it carries instead of vanishing. */
export function lineText(m: Pick<CanonicalRoomMessage, "text" | "attachments" | "taskRefs">): string {
  if (m.text?.trim()) return m.text;
  if (m.attachments?.length) return `Attached: ${m.attachments.map((a) => a.fileName).join(", ")}`;
  if (m.taskRefs?.length) return "Started a task";
  return "(No text)";
}

// ponytail: the newest page only (bounded history); add rooms.loadOlder on scroll-to-top when people ask for more.
export default function ChatScreen() {
  // `title`: the row's own words from Home (a meeting's title, a caller's name), display only; a call room's
  // stored name is an internal slug.
  const { roomId, title } = useLocalSearchParams<{ roomId: string; title?: string }>();
  const palette = useChatPalette();
  const insets = useSafeAreaInsets();
  const keyboardOffset = usePushedScreenKeyboardOffset();
  const [focused, setFocused] = useState(false);
  useFocusEffect(useCallback(() => { setFocused(true); return () => setFocused(false); }, []));
  const rooms = useMobileRooms({ subscribeToRoom: focused, autoSelect: false, visible: focused, boundedHistory: true });
  // selectRoom's identity changes with every message, so it goes through a ref.
  const select = useRef(rooms.selectRoom);
  select.current = rooms.selectRoom;
  useEffect(() => { select.current(roomId ?? null); }, [roomId]);
  const open = !!roomId && rooms.activeRoomId === roomId;
  // The composer is only for the member's own text chats, decided from the room itself (kind and members).
  // A call or meeting room is never in that shape, so it is a record; until the room is known, nothing is writable.
  const room = open ? rooms.activeRoom ?? rooms.rooms.find((r) => r.roomId === roomId) : undefined;
  const writable = isTextChat(room);
  const replying = open && rooms.runs.some((run) => run.roomId === roomId && run.state === "running");
  const [text, setText] = useState("");
  // A failed send stays on screen as its own "Not sent" row, apart from the composer, so typing the next message
  // never loses it; a tap resends it with the same id, so the server's idempotency receipt dedupes an uncertain one.
  const [unsent, setUnsent] = useState<{ id: string; text: string }[]>([]);
  const send = async (body: string, clientMessageId: string = randomUUID()) => {
    if (!body || !open || !writable) return;
    setUnsent((rows) => rows.filter((r) => r.id !== clientMessageId));
    try {
      await rooms.sendMessage({ clientMessageId, text: body, mentions: [], replyToMessageId: null, threadRootMessageId: null });
    } catch {
      setUnsent((rows) => [...rows, { id: clientMessageId, text: body }]);
    }
  };
  const submit = () => { const body = text.trim(); if (!body) return; setText(""); void send(body); };

  const rows: Row[] = open ? [
    ...rooms.messages.filter((m) => !m.deletedAt)
      .map((m) => ({ key: m.messageId, human: writable && m.author.kind === "human", name: m.author.displayName ?? "", text: lineText(m) })),
    ...rooms.optimisticMessages.map((p) => ({ key: p.clientMessageId, human: true, name: "", text: p.text, pending: true })),
    ...unsent.map((u) => ({ key: u.id, human: true, name: "", text: u.text, unsent: true })),
  ].reverse() : [];

  const fg = { color: palette.fg, fontFamily: fonts.sans, fontSize: 16, lineHeight: 23 };
  return (
    <KeyboardAvoidingView behavior="padding" keyboardVerticalOffset={keyboardOffset} style={[styles.page, { backgroundColor: palette.ground }]}>
      <Stack.Screen options={{ title: writable ? "Lightbulb" : title || room?.name || "Record" }} />
      <FlatList inverted data={rows} keyExtractor={(row) => row.key} contentContainerStyle={styles.list}
        keyboardDismissMode="interactive"
        ListHeaderComponent={replying && writable ? <Text accessibilityRole="text" accessibilityLiveRegion="polite" style={[fg, { color: palette.fgMute }]}>Lightbulb is replying…</Text> : null}
        ListEmptyComponent={<Text style={[fg, styles.empty, { color: palette.fgMute }]}>{!open ? "Loading…" : writable ? "Ask Lightbulb anything." : "Nothing was recorded here."}</Text>}
        renderItem={({ item }) => item.unsent ? (
          <Pressable accessibilityRole="button" accessibilityLabel={`Not sent: ${item.text}. Tap to retry`}
            onPress={() => void send(item.text, item.key)} style={[styles.mine, { backgroundColor: palette.selected }]}>
            <Text style={fg}>{item.text}</Text>
            <Text style={[fg, styles.error, { color: palette.alert }]}>Not sent. Tap to retry</Text>
          </Pressable>
        ) : item.human ? (
          <View style={[styles.mine, { backgroundColor: palette.selected, opacity: item.pending ? 0.6 : 1 }]}>
            <Text selectable style={fg}>{item.text}</Text>
          </View>
        ) : (
          <View style={styles.theirs}>
            <Text style={[fg, styles.name, { color: palette.fgMute }]}>{item.name}</Text>
            <Markdown style={{ body: fg, link: { color: palette.link } }}>{item.text}</Markdown>
          </View>
        )} />
      {writable ? (
        <>
          <View style={[styles.composer, { borderColor: palette.edge, paddingBottom: Math.max(insets.bottom, spacing.sm) }]}>
            <TextInput value={text} onChangeText={setText} multiline accessibilityLabel="Message" placeholder="Message Lightbulb"
              placeholderTextColor={palette.fgMute} style={[fg, styles.input, { borderColor: palette.edge }]} />
            <Pressable accessibilityRole="button" accessibilityLabel="Send" accessibilityState={{ disabled: !text.trim() || !open }}
              disabled={!text.trim() || !open} onPress={submit} hitSlop={6}
              style={({ pressed }) => [styles.send, { backgroundColor: palette.fg, opacity: pressed || !text.trim() || !open ? 0.5 : 1 }]}>
              <Text style={{ color: palette.onFg, fontFamily: fonts.sansSemiBold, fontSize: 15 }}>Send</Text>
            </Pressable>
          </View>
        </>
      ) : <View style={{ height: insets.bottom }} />}
    </KeyboardAvoidingView>
  );
}

const styles = StyleSheet.create({
  page: { flex: 1 },
  list: { padding: spacing.lg, gap: spacing.md },
  empty: { textAlign: "center", marginTop: spacing.xl },
  mine: { alignSelf: "flex-end", maxWidth: "85%", borderRadius: radii.md, paddingHorizontal: spacing.md, paddingVertical: spacing.sm },
  theirs: { alignSelf: "stretch" },
  name: { fontSize: 13, lineHeight: 18 },
  error: { fontSize: 14 },
  composer: { flexDirection: "row", alignItems: "flex-end", gap: spacing.sm, paddingHorizontal: spacing.md, paddingTop: spacing.sm, borderTopWidth: StyleSheet.hairlineWidth },
  input: { flex: 1, minHeight: 44, maxHeight: 140, borderWidth: 1, borderRadius: radii.md, paddingHorizontal: spacing.md, paddingVertical: spacing.sm },
  send: { minHeight: 44, minWidth: 64, borderRadius: 999, alignItems: "center", justifyContent: "center", paddingHorizontal: spacing.md },
});
