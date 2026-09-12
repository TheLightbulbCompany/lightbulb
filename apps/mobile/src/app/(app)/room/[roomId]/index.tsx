import { useAuth } from "@clerk/clerk-expo";
import Feather from "@expo/vector-icons/Feather";
import {
  buildTimeline,
  roomDisplayName,
  runView,
  type CanonicalRoomMessage,
  type MemberPrefsRequest,
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
import { HeaderBack } from "../../../../components/room/HeaderBack";
import { MessageActionsSheet } from "../../../../components/room/MessageActionsSheet";
import { LocalMessageRows, MessageGroup } from "../../../../components/room/MessageGroup";
import { RoomBlockedState } from "../../../../components/room/RoomBlockedState";
import { RoomComposer, useComposerKeyboardOffset } from "../../../../components/room/RoomComposer";
import { RoomHeaderActions } from "../../../../components/room/RoomHeaderActions";
import { RunBlock } from "../../../../components/room/RunBlock";
import { colors, fonts, radii, spacing, type } from "../../../../config/theme";
import { claimRoomTarget } from "../../../../lib/activeAgent";
import { useMobileRooms, useSendDrafts, useTypingReport } from "../../../../lib/rooms";
import { typingLine } from "../../../../lib/typingLine";

/** RoomHeader.tsx's own copy — the same two strings the room list's sheet shows. */
const STAR_FAILED = "The star could not be saved.";
const MUTE_FAILED = "The notification setting could not be saved.";

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

  // Star/mute from the header's icon buttons. A native header has no room
  // below the buttons for an inline error, so this screen owns the failure
  // copy and shows it in the body instead (RoomHeader.tsx's own two strings).
  const [prefsError, setPrefsError] = useState<string | null>(null);
  const { setRoomPrefs } = rooms;
  const savePrefs = useCallback(
    async (prefs: MemberPrefsRequest, failureMessage: string) => {
      if (!roomId) return;
      setPrefsError(null);
      try {
        await setRoomPrefs(roomId, prefs);
      } catch {
        setPrefsError(failureMessage);
      }
    },
    [roomId, setRoomPrefs],
  );

  // In-flight and failed sends, drawn through the same MessageGroup as a real
  // message. On a fast network the server answers first; on a slow one this is
  // the difference between "sent" and "nothing happened".
  const { canSend, send, retry, rows } = useSendDrafts(rooms, {
    authorId: userId ?? "",
    authorName: viewer?.displayName ?? "You",
    threadRootMessageId: null,
  });
  const reportTyping = useTypingReport(rooms.setTyping, focused);
  const typingText = typingLine(rooms.typingMembers.filter((member) => member.userId !== userId));

  // The message a long press is acting on; dropping it dismisses the sheet.
  const [actionTarget, setActionTarget] = useState<CanonicalRoomMessage | null>(null);

  // The messageId of the most recent failed reaction toggle (mirrors the web's
  // single `reactionError` string state) — cleared UNCONDITIONALLY at the start
  // of every new attempt (matching RoomPanel.toggleReaction's timing exactly:
  // `setReactionError(null)` before the call, not only after a success), from
  // either the reactions row or the long-press sheet.
  const [reactionErrorMessageId, setReactionErrorMessageId] = useState<string | null>(null);
  const reactToMessage = useCallback(
    (messageId: string, emoji: string, active: boolean) => {
      setReactionErrorMessageId(null);
      void rooms.setReaction(messageId, emoji, active).catch(() => setReactionErrorMessageId(messageId));
    },
    [rooms],
  );

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

  // A reader with no access, or a room that failed to open, replaces the WHOLE
  // panel — timeline and composer both — the same guard web's RoomPanel runs
  // (search `room-blocked`). `accessRemoved` means the server rotated the
  // room's delivery key after removing this reader; the composer must go, not
  // just sit disabled over a stale transcript.
  // Hoisted above the blocked-state early return below: a hook called inside
  // the JSX prop would run on the normal render and not on a blocked one,
  // which is a rules-of-hooks violation and a real order change, not a lint nit.
  const keyboardOffset = useComposerKeyboardOffset();

  const headerOptions = {
    title,
    // The room is this Stack's root, so nothing draws a back button for it.
    // It lives on `headerOptions` rather than inline so the blocked-state
    // early return below keeps it too — a blocked screen with no way out is
    // worse than the state it is reporting.
    headerLeft: () => <HeaderBack fallback="/(app)/home" />,
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
    headerRight: () => (
      <RoomHeaderActions
        starred={!!rooms.activeRoom?.starred}
        muted={!!rooms.activeRoom?.muted}
        disabled={!rooms.activeRoom}
        onToggleStar={() => void savePrefs({ starred: !rooms.activeRoom?.starred }, STAR_FAILED)}
        onToggleMute={() => void savePrefs({ muted: !rooms.activeRoom?.muted }, MUTE_FAILED)}
      />
    ),
  };
  if (rooms.roomLoading || rooms.accessRemoved || rooms.roomError || rooms.selectRoomError) {
    const blockedState = rooms.roomLoading
      ? "loading" as const
      : rooms.accessRemoved
        ? "access-removed" as const
        : "unavailable" as const;
    return (
      <View style={styles.container}>
        <Stack.Screen options={headerOptions} />
        <RoomBlockedState
          state={blockedState}
          onRetry={blockedState === "unavailable" ? rooms.refreshRoom : undefined}
        />
      </View>
    );
  }

  return (
    <KeyboardAvoidingView
      style={styles.container}
      behavior={Platform.OS === "ios" ? "padding" : undefined}
      keyboardVerticalOffset={keyboardOffset}
    >
      <Stack.Screen options={headerOptions} />
      {prefsError && (
        <Text accessibilityRole="alert" style={styles.prefsError}>
          {prefsError}
        </Text>
      )}
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
              <Text style={styles.errorText}>{rooms.historyErrorMessage ? `Couldn't load history: ${rooms.historyErrorMessage}` : "Couldn't load history."}</Text>
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
              members={members ?? []}
              currentUserId={userId ?? ""}
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
              currentUserId={userId ?? ""}
              reactionError={reactionErrorMessageId === item.message.messageId}
              onToggleReaction={(emoji, active) =>
                reactToMessage(item.message.messageId, emoji, active)
              }
              onLongPress={() => setActionTarget(item.message)}
              onOpenThread={() =>
                router.push(`/(app)/room/${roomId}/thread/${item.message.messageId}`)
              }
              downloadAttachment={rooms.downloadAttachment}
              members={members ?? []}
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
          // ponytail: mark-unread's rejection is still swallowed, so a failed
          // mark-unread just looks like nothing happened; the upgrade is an
          // inline error or a toast. Reaction failures now surface (see
          // `reactionErrorMessageId` above), matching the web's `reactionError`.
          onReact={(emoji, active) => reactToMessage(actionTarget.messageId, emoji, active)}
          onReplyInThread={() =>
            router.push(`/(app)/room/${roomId}/thread/${actionTarget.messageId}`)
          }
          onMarkUnread={() =>
            void rooms.markUnread(roomId, actionTarget.messageSeq).catch(() => {})
          }
        />
      )}
      {typingText ? <Text style={styles.typing}>{typingText}</Text> : null}
      <RoomComposer
        members={members ?? []}
        placeholder="Message"
        disabled={!canSend}
        reconnecting={!rooms.liveConnected}
        onSend={send}
        onUploadAttachment={rooms.uploadAttachment}
        onAbort={runningRun ? () => void rooms.abortRun(runningRun.runId).catch(() => {}) : null}
        onTypingChange={reportTyping}
      />
    </KeyboardAvoidingView>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: colors.cream },
  list: { paddingHorizontal: spacing.md, paddingTop: spacing.sm, paddingBottom: spacing.lg },
  listEmpty: { flexGrow: 1 },
  headerTitle: { alignItems: "center" },
  headerName: { fontFamily: fonts.sansSemiBold, fontSize: 17, lineHeight: 22, color: colors.ink },
  headerMeta: { fontFamily: fonts.sans, fontSize: 11, lineHeight: 14, color: colors.mute },
  prefsError: {
    fontFamily: fonts.sans,
    fontSize: 13,
    lineHeight: 18,
    color: colors.errorText,
    paddingHorizontal: spacing.lg,
    paddingTop: spacing.sm,
  },
  divider: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.md,
    marginTop: spacing.md,
    marginBottom: spacing.xs,
  },
  rule: { flex: 1, height: StyleSheet.hairlineWidth, backgroundColor: colors.edge },
  dividerLabel: {
    fontFamily: fonts.sansMedium,
    fontSize: 12,
    lineHeight: 16,
    color: colors.mute,
  },
  newRule: { backgroundColor: colors.info },
  newLabel: {
    fontFamily: fonts.sansSemiBold,
    fontSize: 12,
    lineHeight: 16,
    color: colors.info,
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
  typing: { ...type.caption, paddingHorizontal: spacing.lg, paddingTop: spacing.xs },
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
