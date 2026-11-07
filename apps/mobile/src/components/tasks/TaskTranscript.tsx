// A thread's conversation: its hidden task room (contracts-v2 §6) through the ordinary room machine,
// with a composer whose message steers the thread. No threads-of-threads, no runs: the task's own lines.

import { useAuth } from "@clerk/expo";
import { buildTimeline, cardAnswersFor } from "@lightbulb/shared";
import { useEffect, useMemo, useRef } from "react";
import { FlatList, StyleSheet, Text, View } from "react-native";

import { spacing, useChatPalette, fonts } from "../../config/theme";
import { useDraftKey } from "../../lib/composerDraft";
import { cardControls } from "../../lib/cardControls";
import { cardReplySender, useMobileRooms, useSendDrafts, useTypingReport } from "../../lib/rooms";
import { MetaText } from "../conversation/bubbles";
import { LocalMessageRows, MessageGroup } from "../room/MessageGroup";
import { RoomComposer } from "../room/RoomComposer";

export function TaskTranscript({ taskRoomId, focused, onSteered }: {
  taskRoomId: string;
  focused: boolean;
  /** A steer went out (analytics, nothing else). */
  onSteered?: () => void;
}) {
  const { userId } = useAuth();
  const palette = useChatPalette();
  const rooms = useMobileRooms({ subscribeToRoom: focused, autoSelect: false, visible: focused, boundedHistory: true });
  const selectRoomRef = useRef(rooms.selectRoom);
  selectRoomRef.current = rooms.selectRoom;
  useEffect(() => { selectRoomRef.current(taskRoomId); }, [taskRoomId]);

  const messages = rooms.activeRoom?.roomId === taskRoomId ? rooms.messages : [];
  const items = useMemo(() => buildTimeline(messages, { readSeq: null }), [messages]);
  const newestFirst = useMemo(() => [...items].reverse(), [items]);
  const members = rooms.activeRoom?.members ?? [];
  const viewer = members.find((member) => member.kind === "human" && member.id === userId);
  const { canSend, send, retry, rows } = useSendDrafts(rooms, { authorId: userId ?? "", authorName: viewer?.displayName ?? "You" });
  const { cardsEnabled, repliesEnabled } = cardControls(rooms.activeRoom ?? undefined);
  const onCardReply = useMemo(() => cardReplySender(send), [send]);
  const cardAnswers = useMemo(() => cardAnswersFor(messages), [messages]);
  const reportTyping = useTypingReport(rooms.setTyping, focused);
  const draftKey = useDraftKey(taskRoomId, null);

  return (
    <View style={styles.host} accessibilityLabel="Task progress">
      <FlatList
        testID="task-transcript"
        inverted
        data={newestFirst}
        keyExtractor={(item) => item.key}
        contentContainerStyle={styles.list}
        keyboardDismissMode="interactive"
        keyboardShouldPersistTaps="handled"
        ListHeaderComponent={<LocalMessageRows rows={rows} onRetry={(id) => void retry(id)} attachmentUrl={rooms.attachmentUrl} members={members} currentUserId={userId ?? ""} />}
        ListEmptyComponent={rooms.historyLoading ? null : (
          <Text style={[styles.empty, { color: palette.fgMute }]}>{"Lightbulb posts this task's progress here. Anything you write steers it."}</Text>
        )}
        renderItem={({ item }) => {
          if (item.kind === "date") return <View style={styles.date}><MetaText palette={palette}>{item.label}</MetaText></View>;
          if (item.kind === "new") return null;
          return (
            <MessageGroup message={item.message} collapsed={item.collapsed} currentUserId={userId ?? ""}
              attachmentUrl={rooms.attachmentUrl} members={members} cardsEnabled={cardsEnabled}
              onCardReply={repliesEnabled && canSend ? onCardReply : undefined} cardAnswer={cardAnswers.get(item.message.messageId) ?? null} />
          );
        }}
      />
      <RoomComposer
        members={members}
        placeholder="Steer this task…"
        disabled={!canSend}
        reconnecting={!rooms.liveConnected}
        onSend={async (draft) => { await send(draft); onSteered?.(); }}
        onUploadAttachment={rooms.uploadAttachment}
        onTypingChange={reportTyping}
        draftKey={draftKey}
        roomKind="channel"
      />
    </View>
  );
}

const styles = StyleSheet.create({
  host: { flex: 1 },
  list: { paddingHorizontal: spacing.lg, paddingTop: spacing.xl },
  date: { alignItems: "center", marginTop: spacing.md, marginBottom: spacing.xs },
  empty: { fontFamily: fonts.sans, fontSize: 14, lineHeight: 20, paddingVertical: spacing.lg, transform: [{ scaleY: -1 }] },
});
