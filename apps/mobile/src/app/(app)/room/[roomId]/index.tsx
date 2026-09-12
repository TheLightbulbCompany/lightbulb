import { useAuth } from "@clerk/clerk-expo";
import Feather from "@expo/vector-icons/Feather";
import {
  buildTimeline,
  roomDisplayName,
  runView,
  type CanonicalRoomMessage,
  type TimelineItem,
} from "@lightbulb/shared";
import { router, Stack, useFocusEffect, useLocalSearchParams } from "expo-router";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  FlatList,
  KeyboardAvoidingView,
  Platform,
  Pressable,
  StyleSheet,
  Text,
  View,
  type NativeScrollEvent,
  type NativeSyntheticEvent,
} from "react-native";

import { EmptyState } from "../../../../components/chat/EmptyState";
import { TimelineSkeleton } from "../../../../components/chat/TimelineSkeleton";
import { MessageActionsSheet } from "../../../../components/room/MessageActionsSheet";
import { LocalMessageRows, MessageGroup } from "../../../../components/room/MessageGroup";
import { RoomComposer } from "../../../../components/room/RoomComposer";
import { RunBlock } from "../../../../components/room/RunBlock";
import { colors, fonts, radii, spacing, type } from "../../../../config/theme";
import { claimRoomTarget } from "../../../../lib/activeAgent";
import { useMobileRooms, useSendDrafts } from "../../../../lib/rooms";

/** Distance from the bottom, in px, still counted as "at the bottom". */
const BOTTOM_SLACK = 80;

export default function ConversationRoute() {
  const { roomId } = useLocalSearchParams<{ roomId: string }>();
  // Remount on a room change: the shared machine keys its history, runs and
  // read cursor off the mounted instance.
  return <Conversation key={roomId ?? "none"} />;
}

function Conversation() {
  const { roomId } = useLocalSearchParams<{ roomId: string }>();
  const { userId } = useAuth();
  // The SCREEN's own focus. expo-router blurs a pushed screen (a thread) without
  // backgrounding the app, and read marking is "on screen AND foregrounded".
  const [focused, setFocused] = useState(true);
  // The same effect also claims the push handler's target — this conversation,
  // no thread — so a message the reader is watching arrive does not banner.
  // One effect, not two: focus and the target are the same event, and the
  // suite's focus mock keeps one handle per screen.
  useFocusEffect(
    useCallback(() => {
      setFocused(true);
      const release = claimRoomTarget(roomId ?? null, null);
      return () => {
        setFocused(false);
        release();
      };
    }, [roomId]),
  );
  const rooms = useMobileRooms({ subscribeToRoom: focused, autoSelect: false, visible: focused });

  // selectRoom's identity changes with every message (it closes over
  // markVisibleRead), and each call bumps the hook's selection-cancellation
  // counter — so fire it on roomId only, through a ref.
  const selectRoomRef = useRef(rooms.selectRoom);
  selectRoomRef.current = rooms.selectRoom;
  useEffect(() => {
    if (roomId) selectRoomRef.current(roomId);
  }, [roomId]);

  // Pinned at open so the "New messages" line does not walk down the list as
  // messages are marked read (mirrors MessagingWorkspaceContext's readSeqAtOpen).
  const readSeqAtOpen = useRef<number | null>(null);
  if (rooms.activeRoom && readSeqAtOpen.current === null) {
    readSeqAtOpen.current = rooms.activeRoom.lastReadSeq;
  }
  const readSeq = readSeqAtOpen.current;

  // Thread replies live on the thread screen, not in the room's own timeline
  // (web RoomPanel does the same split), but they still feed the reply counts.
  const mainMessages = useMemo(
    () => rooms.messages.filter((message) => message.threadRootMessageId === null),
    [rooms.messages],
  );
  const items = useMemo(() => buildTimeline(mainMessages, { readSeq }), [mainMessages, readSeq]);
  const replyCounts = useMemo(() => {
    const counts = new Map<string, number>();
    for (const message of rooms.messages) {
      if (message.threadRootMessageId) {
        counts.set(message.threadRootMessageId, (counts.get(message.threadRootMessageId) ?? 0) + 1);
      }
    }
    return counts;
  }, [rooms.messages]);
  const runs = useMemo(
    () => rooms.runs.filter((run) => !run.threadRootMessageId).map(runView),
    [rooms.runs],
  );

  const members = rooms.activeRoom?.members;
  const viewer = members?.find((member) => member.kind === "human" && member.id === userId);
  const others = (members ?? []).filter(
    (member) => !(member.kind === "human" && member.id === userId),
  );
  const isChannel = rooms.activeRoom?.kind === "channel";
  const dmWithAgent = rooms.activeRoom?.kind === "dm" && others[0]?.kind === "agent";
  // ponytail: Phase A's RoomSummary carries no private flag, so every channel
  // gets `#`; swap in a lock glyph once the backend ships one.
  const title = `${isChannel ? "#" : ""}${roomDisplayName(rooms.activeRoom, others)}`;

  // In-flight and failed sends, drawn through the same MessageGroup as a real
  // message. On a fast network the server answers first; on a slow one this is
  // the difference between "sent" and "nothing happened".
  const { canSend, send, retry, rows } = useSendDrafts(rooms, {
    authorId: userId ?? "",
    authorName: viewer?.displayName ?? "You",
    threadRootMessageId: null,
  });

  // The message a long press is acting on; dropping it dismisses the sheet.
  const [actionTarget, setActionTarget] = useState<CanonicalRoomMessage | null>(null);

  const listRef = useRef<FlatList<TimelineItem>>(null);
  const atBottomRef = useRef(true);
  const [atBottom, setAtBottom] = useState(true);
  const onScroll = useCallback((event: NativeSyntheticEvent<NativeScrollEvent>) => {
    const { contentOffset, contentSize, layoutMeasurement } = event.nativeEvent;
    const next = contentSize.height - layoutMeasurement.height - contentOffset.y < BOTTOM_SLACK;
    atBottomRef.current = next;
    setAtBottom(next);
  }, []);

  const runningRun = runs.find((run) => run.state === "running");
  const agentName = useCallback(
    (agentId: string) =>
      members?.find((member) => member.kind === "agent" && member.id === agentId)?.displayName ??
      "Agent",
    [members],
  );

  return (
    <KeyboardAvoidingView
      style={styles.container}
      behavior={Platform.OS === "ios" ? "padding" : undefined}
      keyboardVerticalOffset={90}
    >
      <Stack.Screen
        options={{
          title,
          headerTitle: () => (
            <View style={styles.headerTitle}>
              <Text style={styles.headerName} numberOfLines={1}>
                {title}
              </Text>
              {isChannel && members ? (
                <Text style={styles.headerMeta}>
                  {members.length} {members.length === 1 ? "member" : "members"}
                </Text>
              ) : null}
            </View>
          ),
        }}
      />
      <FlatList
        ref={listRef}
        testID="room-timeline"
        data={items}
        keyExtractor={(item) => item.key}
        contentContainerStyle={[styles.list, items.length === 0 && styles.listEmpty]}
        keyboardDismissMode="interactive"
        keyboardShouldPersistTaps="handled"
        onScroll={onScroll}
        scrollEventThrottle={16}
        onContentSizeChange={() => {
          if (atBottomRef.current) listRef.current?.scrollToEnd({ animated: true });
        }}
        ListHeaderComponent={
          rooms.historyError ? (
            <View style={styles.errorCard}>
              <Text style={styles.errorText}>Couldn’t load history.</Text>
              <Pressable
                onPress={rooms.loadHistory}
                accessibilityRole="button"
                accessibilityLabel="Retry loading history"
              >
                <Text style={styles.retryText}>Retry</Text>
              </Pressable>
            </View>
          ) : null
        }
        ListEmptyComponent={
          rooms.historyLoading ? (
            <TimelineSkeleton />
          ) : (
            <EmptyState agentName={title} subtitle={dmWithAgent ? undefined : "No messages yet"} />
          )
        }
        ListFooterComponent={
          <View>
            <LocalMessageRows
              rows={rows}
              onRetry={(clientMessageId) => void retry(clientMessageId)}
              downloadAttachment={rooms.downloadAttachment}
            />
            {runs.map((run) => (
              <RunBlock key={run.runId} run={run} name={agentName(run.agentId)} />
            ))}
          </View>
        }
        renderItem={({ item }) => {
          if (item.kind === "date") {
            return (
              <View style={styles.divider}>
                <View style={styles.rule} />
                <Text style={styles.dividerLabel}>{item.label}</Text>
                <View style={styles.rule} />
              </View>
            );
          }
          if (item.kind === "new") {
            return (
              <View style={styles.divider}>
                <View style={[styles.rule, styles.newRule]} />
                <Text style={styles.newLabel}>New messages</Text>
                <View style={[styles.rule, styles.newRule]} />
              </View>
            );
          }
          return (
            <MessageGroup
              message={item.message}
              collapsed={item.collapsed}
              replyCount={replyCounts.get(item.message.messageId) ?? 0}
              onLongPress={() => setActionTarget(item.message)}
              onOpenThread={() =>
                router.push(`/(app)/room/${roomId}/thread/${item.message.messageId}`)
              }
              downloadAttachment={rooms.downloadAttachment}
            />
          );
        }}
      />
      {!atBottom && (
        <Pressable
          style={styles.jump}
          onPress={() => listRef.current?.scrollToEnd({ animated: true })}
          accessibilityRole="button"
          accessibilityLabel="Jump to latest"
        >
          <Feather name="arrow-down" size={16} color={colors.white} />
        </Pressable>
      )}
      {actionTarget && (
        <MessageActionsSheet
          message={actionTarget}
          currentUserId={userId ?? ""}
          roomId={roomId}
          onClose={() => setActionTarget(null)}
          // ponytail: both rejections are swallowed, so a failed reaction or
          // mark-unread just looks like nothing happened; the upgrade is an
          // inline error under the message (the web's `reactionError`) or a toast.
          onReact={(emoji, active) =>
            void rooms.setReaction(actionTarget.messageId, emoji, active).catch(() => {})
          }
          onReplyInThread={() =>
            router.push(`/(app)/room/${roomId}/thread/${actionTarget.messageId}`)
          }
          onMarkUnread={() =>
            void rooms.markUnread(roomId, actionTarget.messageSeq).catch(() => {})
          }
        />
      )}
      <RoomComposer
        members={members ?? []}
        placeholder="Message"
        disabled={!canSend}
        reconnecting={!rooms.liveConnected}
        onSend={send}
        onUploadAttachment={rooms.uploadAttachment}
        onAbort={runningRun ? () => void rooms.abortRun(runningRun.runId).catch(() => {}) : null}
      />
    </KeyboardAvoidingView>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: colors.cream },
  list: { paddingHorizontal: spacing.lg, paddingTop: spacing.lg, paddingBottom: spacing.xl },
  listEmpty: { flexGrow: 1 },
  headerTitle: { alignItems: "center" },
  headerName: { fontFamily: fonts.sansSemiBold, fontSize: 17, lineHeight: 22, color: colors.ink },
  headerMeta: { fontFamily: fonts.sans, fontSize: 11, lineHeight: 14, color: colors.mute },
  divider: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.md,
    marginTop: spacing.xl,
    marginBottom: spacing.xs,
  },
  rule: { flex: 1, height: StyleSheet.hairlineWidth, backgroundColor: colors.edge },
  dividerLabel: type.overline,
  newRule: { backgroundColor: colors.danger },
  newLabel: {
    fontFamily: fonts.sansMedium,
    fontSize: 11,
    lineHeight: 14,
    color: colors.danger,
  },
  errorCard: {
    backgroundColor: colors.errorBg,
    borderWidth: 1,
    borderColor: colors.errorBorder,
    borderRadius: radii.md,
    padding: spacing.lg,
    marginBottom: spacing.lg,
    gap: spacing.sm,
  },
  errorText: { fontFamily: fonts.sans, fontSize: 14, lineHeight: 20, color: colors.errorText },
  retryText: { fontFamily: fonts.sansMedium, fontSize: 13, lineHeight: 18, color: colors.forest },
  jump: {
    position: "absolute",
    right: spacing.lg,
    bottom: 96,
    width: 36,
    height: 36,
    borderRadius: 18,
    backgroundColor: colors.forest,
    alignItems: "center",
    justifyContent: "center",
  },
});
