import { useAuth } from "@clerk/clerk-expo";
import {
  buildTimeline,
  roomDisplayName,
  runView,
  type CanonicalRoomMessage,
} from "@lightbulb/shared";
import { Stack, useFocusEffect, useLocalSearchParams } from "expo-router";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  FlatList,
  KeyboardAvoidingView,
  Platform,
  Pressable,
  StyleSheet,
  Text,
  View,
} from "react-native";

import { TimelineSkeleton } from "../../../../../components/chat/TimelineSkeleton";
import { HeaderBack } from "../../../../../components/room/HeaderBack";
import { MessageActionsSheet } from "../../../../../components/room/MessageActionsSheet";
import { LocalMessageRows, MessageGroup } from "../../../../../components/room/MessageGroup";
import {
  RoomComposer,
  useComposerKeyboardOffset,
} from "../../../../../components/room/RoomComposer";
import { RunBlock } from "../../../../../components/room/RunBlock";
import { colors, fonts, spacing, type } from "../../../../../config/theme";
import { claimRoomTarget } from "../../../../../lib/activeAgent";
import { useMobileRooms, useSendDrafts } from "../../../../../lib/rooms";

/**
 * One thread: its root pinned above the list, its replies below in the same item
 * renderer the room uses, and a composer that sends back into it.
 *
 * Slack's "Also send to #channel" toggle is out of scope this round (spec).
 */
export default function ThreadScreen() {
  const { roomId, messageId } = useLocalSearchParams<{ roomId: string; messageId: string }>();
  const { userId } = useAuth();
  // The SCREEN's own focus, driving the live subscription only: a blurred thread
  // hands the room's subscription claim back to the conversation behind it.
  const [focused, setFocused] = useState(true);
  // The same effect claims the push handler's target: a reply to THIS thread is
  // on screen and banners nothing, while a new message in the room behind it
  // still does.
  useFocusEffect(
    useCallback(() => {
      setFocused(true);
      const release = claimRoomTarget(roomId ?? null, messageId ?? null);
      return () => {
        setFocused(false);
        release();
      };
    }, [roomId, messageId]),
  );
  // `visible: false`, unconditionally — a thread is not the room, and Slack does
  // not clear a channel's unread badge because you read one of its threads. The
  // read cursor is per room, so marking read here would mark messages the user
  // has never seen. The conversation screen picks the marking back up on its
  // visible-flip listener the moment this screen is popped.
  // ponytail: the residual room badge until the conversation is opened is the
  // accepted ceiling; per-thread read state is Phase B and needs a backend field.
  const rooms = useMobileRooms({ subscribeToRoom: focused, autoSelect: false, visible: false });

  // selectRoom's identity changes with every message (it closes over
  // markVisibleRead), and each call bumps the hook's selection-cancellation
  // counter — so fire it on roomId only, through a ref.
  const selectRoomRef = useRef(rooms.selectRoom);
  selectRoomRef.current = rooms.selectRoom;
  useEffect(() => {
    if (roomId) selectRoomRef.current(roomId);
  }, [roomId]);

  const root = rooms.messages.find((message) => message.messageId === messageId) ?? null;
  const replies = useMemo(
    () => rooms.messages.filter((message) => message.threadRootMessageId === messageId),
    [rooms.messages, messageId],
  );
  // readSeq is null on purpose: a thread has no "New messages" line in Slack,
  // and the room's own read cursor counts the room's sequence, not this thread's.
  const items = useMemo(() => buildTimeline(replies, { readSeq: null }), [replies]);
  const runs = useMemo(
    () => rooms.runs.filter((run) => run.threadRootMessageId === messageId).map(runView),
    [rooms.runs, messageId],
  );

  const members = rooms.activeRoom?.members;
  const viewer = members?.find((member) => member.kind === "human" && member.id === userId);
  const others = (members ?? []).filter(
    (member) => !(member.kind === "human" && member.id === userId),
  );
  const isChannel = rooms.activeRoom?.kind === "channel";
  const subtitle = `${isChannel ? "#" : ""}${roomDisplayName(rooms.activeRoom, others)}`;

  const { canSend, send, retry, rows } = useSendDrafts(rooms, {
    authorId: userId ?? "",
    authorName: viewer?.displayName ?? "You",
    threadRootMessageId: messageId ?? null,
  });

  // The message a long press is acting on; dropping it dismisses the sheet.
  const [actionTarget, setActionTarget] = useState<CanonicalRoomMessage | null>(null);

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
      keyboardVerticalOffset={useComposerKeyboardOffset()}
    >
      <Stack.Screen
        options={{
          title: "Thread",
          // A push can deep-link straight into a thread, with no room beneath
          // it to pop back to — hence the explicit fallback.
          headerLeft: () => <HeaderBack fallback={`/(app)/room/${roomId}`} />,
        }}
      />
      <View style={styles.subtitleRow}>
        <Text style={styles.subtitle} numberOfLines={1}>
          {subtitle}
        </Text>
      </View>
      <FlatList
        testID="thread-timeline"
        data={items}
        keyExtractor={(item) => item.key}
        contentContainerStyle={styles.list}
        keyboardDismissMode="interactive"
        keyboardShouldPersistTaps="handled"
        ListHeaderComponent={
          root ? (
            <View>
              <MessageGroup
                message={root}
                collapsed={false}
                replyCount={0}
                onLongPress={() => setActionTarget(root)}
                downloadAttachment={rooms.downloadAttachment}
              />
              <View style={styles.hairline} />
              <Text style={styles.count}>
                {replies.length} {replies.length === 1 ? "reply" : "replies"}
              </Text>
            </View>
          ) : rooms.historyLoading ? (
            <TimelineSkeleton />
          ) : (
            <View style={styles.missing}>
              <Text style={styles.missingText}>Couldn’t load this thread.</Text>
              <Pressable
                onPress={rooms.loadHistory}
                accessibilityRole="button"
                accessibilityLabel="Retry loading history"
              >
                <Text style={styles.retryText}>Retry</Text>
              </Pressable>
            </View>
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
          // Unreachable while readSeq is null; buildTimeline's union still has it.
          if (item.kind === "new") return null;
          return (
            <MessageGroup
              message={item.message}
              collapsed={item.collapsed}
              replyCount={0}
              onLongPress={() => setActionTarget(item.message)}
              downloadAttachment={rooms.downloadAttachment}
            />
          );
        }}
      />
      {actionTarget && (
        <MessageActionsSheet
          message={actionTarget}
          currentUserId={userId ?? ""}
          roomId={roomId}
          onClose={() => setActionTarget(null)}
          // ponytail: a rejected reaction is swallowed, so the emoji just never
          // appears; the upgrade is an inline error under the message (what the
          // web's `reactionError` does) or a toast.
          onReact={(emoji, active) =>
            void rooms.setReaction(actionTarget.messageId, emoji, active).catch(() => {})
          }
          // No Reply in thread and no Mark unread — the web's `!inThread` rule.
          // A reply cannot root a nested thread, and the read cursor is per room,
          // so marking unread here is undone the moment the conversation behind
          // this screen is re-focused; the action stays thread-hidden until
          // per-thread read state exists (Phase B, needs a backend field).
        />
      )}
      <RoomComposer
        members={members ?? []}
        placeholder="Reply…"
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
  list: { paddingHorizontal: spacing.lg, paddingBottom: spacing.xl },
  subtitleRow: {
    paddingHorizontal: spacing.lg,
    paddingTop: spacing.sm,
    paddingBottom: spacing.xs,
  },
  subtitle: type.caption,
  rule: { flex: 1, height: StyleSheet.hairlineWidth, backgroundColor: colors.edge },
  hairline: {
    height: StyleSheet.hairlineWidth,
    backgroundColor: colors.edge,
    marginTop: spacing.md,
  },
  count: { ...type.overline, marginTop: spacing.sm },
  divider: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.md,
    marginTop: spacing.xl,
    marginBottom: spacing.xs,
  },
  dividerLabel: type.overline,
  missing: { paddingTop: spacing.xl, gap: spacing.sm },
  missingText: { fontFamily: fonts.sans, fontSize: 14, lineHeight: 20, color: colors.mute },
  retryText: { fontFamily: fonts.sansMedium, fontSize: 13, lineHeight: 18, color: colors.forest },
});
