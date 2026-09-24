import { useAuth } from "@clerk/clerk-expo";
import { SymbolView } from "expo-symbols";
import {
  buildTimeline,
  roomDisplayName,
  runView,
  type CanonicalRoomMessage,
  type MemberPrefsRequest,
  type TimelineItem,
} from "@lightbulb/shared";
import { router, Stack, useFocusEffect, useIsPreview, useLocalSearchParams } from "expo-router";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  FlatList,
  Pressable,
  StyleSheet,
  Text,
  View,
  type NativeScrollEvent,
  type NativeSyntheticEvent,
} from "react-native";
import { KeyboardAvoidingView } from "react-native-keyboard-controller";

import { EmptyState } from "../../../../components/chat/EmptyState";
import { TimelineSkeleton } from "../../../../components/chat/TimelineSkeleton";
import { MessageActionsSheet } from "../../../../components/room/MessageActionsSheet";
import { LocalMessageRows, MessageGroup } from "../../../../components/room/MessageGroup";
import { OnboardingBand } from "../../../../components/room/OnboardingBand";
import { RoomBlockedState } from "../../../../components/room/RoomBlockedState";
import { RoomComposer } from "../../../../components/room/RoomComposer";
import { RoomHeaderActions } from "../../../../components/room/RoomHeaderActions";
import { RunBlock } from "../../../../components/room/RunBlock";
import { colors, fonts, radii, spacing, type } from "../../../../config/theme";
import { claimRoomTarget } from "../../../../lib/activeAgent";
import { usePushedScreenKeyboardOffset } from "../../../../lib/keyboardOffset";
import { AnimatedPressable, usePressScale } from "../../../../lib/pressScale";
import { useDraftKey } from "../../../../lib/composerDraft";
import { useMobileRooms, useSendDrafts, useTypingReport } from "../../../../lib/rooms";
import { typingLine } from "../../../../lib/typingLine";

/** RoomHeader.tsx's own copy — the same two strings the room list shows. */
const STAR_FAILED = "The star could not be saved.";
const MUTE_FAILED = "The notification setting could not be saved.";

/**
 * How far the reader may scroll back, in px, before the jump-to-latest pill
 * appears. The list is INVERTED, so `contentOffset.y === 0` is the newest
 * message and the offset grows going back in time — no content/layout
 * arithmetic, and nothing to recompute when the keyboard resizes the list.
 */
const JUMP_THRESHOLD = 80;

export default function ConversationRoute() {
  const { roomId } = useLocalSearchParams<{ roomId: string }>();
  // Remount on a room change: the shared machine keys its history, runs and
  // read cursor off the mounted instance.
  return <Conversation key={roomId ?? "none"} />;
}

function Conversation() {
  const { roomId } = useLocalSearchParams<{ roomId: string }>();
  const { userId } = useAuth();
  // A long press on a room-list row peeks this screen (Link.Preview). A peek
  // is read-only: its navigation always reports focus, so it opts out of the
  // focus effect below entirely — no read marking, no live subscription, no
  // push-target claim — and it has no header of its own to set.
  const preview = useIsPreview();
  // The SCREEN's own focus. expo-router blurs a pushed screen (a thread) without
  // backgrounding the app, and read marking is "on screen AND foregrounded".
  // Starts false: the same peek preloads this route into the stack unfocused,
  // and it stays mounted there after the peek is dismissed.
  const [focused, setFocused] = useState(false);
  // The same effect also claims the push handler's target — this conversation,
  // no thread — so a message the reader is watching arrive does not banner.
  // One effect, not two: focus and the target are the same event, and the
  // suite's focus mock keeps one handle per screen.
  useFocusEffect(
    useCallback(() => {
      if (preview) return;
      setFocused(true);
      const release = claimRoomTarget(roomId ?? null, null);
      return () => {
        setFocused(false);
        release();
      };
    }, [roomId, preview]),
  );
  const rooms = useMobileRooms({ subscribeToRoom: focused, autoSelect: false, visible: focused, boundedHistory: true });
  // The room's own timeline drafts separately from each of its threads.
  const composerDraftKey = useDraftKey(roomId, null);

  // selectRoom's identity changes with every message (it closes over
  // markVisibleRead), and each call bumps the hook's selection-cancellation
  // counter — so it goes through a ref, and only fires when the answer below
  // actually changes.
  const selectRoomRef = useRef(rooms.selectRoom);
  selectRoomRef.current = rooms.selectRoom;
  // A peek has to select the room: that is what loads the messages it shows.
  // But a dismissed peek leaves this route preloaded and unfocused for the
  // rest of the session, and a selected room polls its detail every 60s — peek
  // a few rows and those polls just accumulate. So a route that was ONLY ever
  // a preview lets go again; one the reader actually opened holds on, because
  // pushing a thread over it unfocuses it too.
  const opened = useRef(false);
  const selection = useRef<string | null>(null);
  useEffect(() => {
    if (focused) opened.current = true;
    const want = roomId && (preview || focused || opened.current) ? roomId : null;
    if (selection.current === want) return;
    selection.current = want;
    selectRoomRef.current(want);
  }, [roomId, preview, focused]);

  // What this screen draws: the room detail once it lands, and until then the
  // room-list entry already in memory, so the header, timeline and composer
  // paint on the first frame instead of behind a detail round trip. Only the
  // live subscription needs the detail (its deliveryKey), and the shared hook
  // subscribes from the detail alone.
  const room = rooms.activeRoom ?? (roomId ? rooms.directoryRoomDetails.get(roomId) : undefined);

  // Pinned at open so the "New messages" line does not walk down the list as
  // messages are marked read (mirrors MessagingWorkspaceContext's readSeqAtOpen).
  const readSeqAtOpen = useRef<number | null>(null);
  if (room && readSeqAtOpen.current === null) {
    readSeqAtOpen.current = room.lastReadSeq;
  }
  const readSeq = readSeqAtOpen.current;

  // Thread replies live on the thread screen, not in the room's own timeline
  // (web RoomPanel does the same split), but they still feed the reply counts.
  const mainMessages = useMemo(
    () => rooms.messages.filter((message) => message.threadRootMessageId === null),
    [rooms.messages],
  );
  const items = useMemo(() => buildTimeline(mainMessages, { readSeq }), [mainMessages, readSeq]);
  // `buildTimeline` is oldest → newest; an inverted list draws index 0 at the
  // bottom, so reversing here puts the same visual order on screen — dividers
  // included — with the newest message simply THERE, never scrolled to.
  const newestFirst = useMemo(() => [...items].reverse(), [items]);
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

  const members = room?.members;
  const viewer = members?.find((member) => member.kind === "human" && member.id === userId);
  const others = (members ?? []).filter(
    (member) => !(member.kind === "human" && member.id === userId),
  );
  const isChannel = room?.kind === "channel";
  const dmWithAgent = room?.kind === "dm" && others[0]?.kind === "agent";
  // ponytail: Phase A's RoomSummary carries no private flag, so every channel
  // gets `#`; swap in a lock glyph once the backend ships one.
  const title = `${isChannel ? "#" : ""}${roomDisplayName(room, others)}`;

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

  // The message a long press is acting on; dropping it starts the sheet's
  // close animation. `lastActionTarget` keeps the sheet's content stable
  // through that close — BottomSheet stays mounted and is only ever toggled
  // via `visible`, so `MessageActionsSheet` needs a non-null `message` for
  // its whole exit animation, not just up to the moment the target clears.
  const [actionTarget, setActionTarget] = useState<CanonicalRoomMessage | null>(null);
  const lastActionTarget = useRef<CanonicalRoomMessage | null>(null);
  if (actionTarget) lastActionTarget.current = actionTarget;

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
  const [atBottom, setAtBottom] = useState(true);
  const jumpScale = usePressScale();
  const onScroll = useCallback((event: NativeSyntheticEvent<NativeScrollEvent>) => {
    setAtBottom(event.nativeEvent.contentOffset.y <= JUMP_THRESHOLD);
  }, []);

  const runningRun = runs.find((run) => run.state === "running");
  // An agent already working answers whether or not this message addresses it.
  const runningAgentIds = useMemo(
    () => runs.filter((run) => run.state === "running").map((run) => run.agentId),
    [runs],
  );
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
  const keyboardOffset = usePushedScreenKeyboardOffset();

  // On `headerOptions` rather than inline so the blocked-state early return
  // below keeps the header too — a blocked screen with no title and no
  // star/mute is worse than the state it is reporting. The back button is the
  // (app) Stack's own: the room is a PUSHED screen now, not a Stack root.
  const headerOptions = {
    title,
    // Tapping the title opens the room's details, as in Slack.
    headerTitle: () => (
      <Pressable
        style={styles.headerTitle}
        disabled={!roomId}
        onPress={() => router.push(`/(app)/room/${roomId}/details`)}
        accessibilityRole="button"
        accessibilityHint="Shows members and conversation settings"
      >
        {/* Fixed like UIKit's own navigation titles, which do not scale with
            the reader's text size — the bar's height does not grow either. */}
        <Text style={styles.headerName} numberOfLines={1} maxFontSizeMultiplier={1}>
          {title}
        </Text>
        {isChannel && members ? (
          <Text style={styles.headerMeta} numberOfLines={1} maxFontSizeMultiplier={1}>
            {members.length} {members.length === 1 ? "member" : "members"}
            {room?.shared ? " · Shared with Lightbulb" : ""}
          </Text>
        ) : null}
      </Pressable>
    ),
    headerRight: () => (
      <RoomHeaderActions
        starred={!!room?.starred}
        muted={!!room?.muted}
        disabled={!room}
        onToggleStar={() => void savePrefs({ starred: !room?.starred }, STAR_FAILED)}
        onToggleMute={() => void savePrefs({ muted: !room?.muted }, MUTE_FAILED)}
      />
    ),
  };
  const roomLoading = rooms.roomLoading && !room;
  if (roomLoading || rooms.accessRemoved || rooms.roomError || rooms.selectRoomError) {
    const blockedState = roomLoading
      ? "loading" as const
      : rooms.accessRemoved
        ? "access-removed" as const
        : "unavailable" as const;
    return (
      <View style={styles.container}>
        {!preview && <Stack.Screen options={headerOptions} />}
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
      behavior="padding"
      keyboardVerticalOffset={keyboardOffset}
    >
      {!preview && <Stack.Screen options={headerOptions} />}
      {prefsError && (
        <Text accessibilityRole="alert" style={styles.prefsError}>
          {prefsError}
        </Text>
      )}
      <FlatList
        ref={listRef}
        testID="room-timeline"
        // Inverted, the way every chat app draws a timeline: the newest content
        // is at offset 0 and simply appears there, so a new message, a run frame
        // or the keyboard resizing the list never animates a scroll.
        inverted
        data={newestFirst}
        keyExtractor={(item) => item.key}
        maintainVisibleContentPosition={{ minIndexForVisible: 0, autoscrollToTopThreshold: 48 }}
        contentContainerStyle={[styles.list, items.length === 0 && styles.listEmpty]}
        keyboardDismissMode="interactive"
        keyboardShouldPersistTaps="handled"
        onScroll={onScroll}
        scrollEventThrottle={16}
        // Header and footer are swapped against the un-inverted version: on an
        // inverted list the HEADER draws at the bottom, under the newest message.
        ListHeaderComponent={
          <View>
            <LocalMessageRows
              rows={rows}
              onRetry={(clientMessageId) => void retry(clientMessageId)}
              downloadAttachment={rooms.downloadAttachment}
              members={members ?? []}
              currentUserId={userId ?? ""}
            />
            {runs.map((run) => (
              <RunBlock
                key={run.runId}
                run={run}
                name={agentName(run.agentId)}
                onAbort={() => rooms.abortRun(run.runId)}
                onSteer={(text) => rooms.steerRun(run.runId, text)}
              />
            ))}
          </View>
        }
        // Upright inside the flipped list: the flip that rights every row is
        // handed to the row through its own `style` prop, which a component
        // that takes no `style` never applies — so the empty state drew upside
        // down. This wrapper takes the flip instead.
        ListEmptyComponent={
          <View style={styles.emptyUpright}>
            {rooms.historyLoading ? (
              <TimelineSkeleton />
            ) : !rooms.historyComplete && rooms.messages.length > 0 ? (
              <Text style={styles.olderHint}>Older messages are available.</Text>
            ) : (
              <EmptyState agentName={title} subtitle={dmWithAgent ? undefined : "No messages yet"} />
            )}
          </View>
        }
        ListFooterComponent={
          <View>
          {rooms.historyError ? (
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
          ) : null}
          {!rooms.historyComplete && rooms.messages.length > 0 && !rooms.historyError ? (
            <Pressable
              onPress={rooms.loadOlder}
              disabled={rooms.historyLoading}
              accessibilityRole="button"
              accessibilityLabel="Load older messages"
              accessibilityState={{ disabled: rooms.historyLoading }}
              style={styles.olderButton}
            >
              <Text style={styles.olderText}>{rooms.historyLoading ? "Loading older messages…" : "Load older messages"}</Text>
            </Pressable>
          ) : null}
          {/* The web band's `leading` slot: above the oldest loaded message. */}
          {!preview && room?.isDefault ? <OnboardingBand participants={members ?? []} /> : null}
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
        <AnimatedPressable
          style={[styles.jump, jumpScale.style]}
          // Offset 0 IS the newest message on an inverted list.
          onPress={() => listRef.current?.scrollToOffset({ offset: 0, animated: true })}
          onPressIn={jumpScale.onPressIn}
          onPressOut={jumpScale.onPressOut}
          accessibilityRole="button"
          accessibilityLabel="Jump to latest"
        >
          <SymbolView accessibilityElementsHidden name="arrow.down" size={16} tintColor={colors.white} />
        </AnimatedPressable>
      )}
      {(() => {
        // A local const so TS can narrow it through the closures below —
        // `lastActionTarget.current` itself doesn't narrow across a nested
        // arrow function boundary, and these must never read `actionTarget`
        // directly: it clears to null the instant a row closes the sheet,
        // before BottomSheet's exit animation finishes.
        const sheetMessage = lastActionTarget.current;
        if (!sheetMessage) return null;
        return (
          <MessageActionsSheet
            message={sheetMessage}
            visible={actionTarget !== null}
            currentUserId={userId ?? ""}
            roomId={roomId}
            onClose={() => setActionTarget(null)}
            // ponytail: mark-unread's rejection is still swallowed, so a failed
            // mark-unread just looks like nothing happened; the upgrade is an
            // inline error or a toast. Reaction failures now surface (see
            // `reactionErrorMessageId` above), matching the web's `reactionError`.
            onReact={(emoji, active) => reactToMessage(sheetMessage.messageId, emoji, active)}
            onReplyInThread={() =>
              router.push(`/(app)/room/${roomId}/thread/${sheetMessage.messageId}`)
            }
            onMarkUnread={() =>
              void rooms.markUnread(roomId, sheetMessage.messageSeq).catch(() => {})
            }
          />
        );
      })()}
      {typingText ? <Text style={styles.typing}>{typingText}</Text> : null}
      <RoomComposer
        members={members ?? []}
        placeholder={`Message ${title}`}
        disabled={!canSend}
        reconnecting={!rooms.liveConnected}
        onSend={send}
        onUploadAttachment={rooms.uploadAttachment}
        onAbort={runningRun ? () => void rooms.abortRun(runningRun.runId).catch(() => {}) : null}
        onTypingChange={reportTyping}
        draftKey={composerDraftKey}
        roomKind={room?.kind}
        runningAgentIds={runningAgentIds}
      />
    </KeyboardAvoidingView>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: colors.cream },
  // Top/bottom are swapped against the un-inverted version: the content
  // container is inside the flipped scroll view, so `paddingTop` draws at the
  // bottom (against the composer) and `paddingBottom` at the top.
  list: { paddingHorizontal: spacing.md, paddingTop: spacing.lg, paddingBottom: spacing.sm },
  listEmpty: { flexGrow: 1 },
  emptyUpright: { flexGrow: 1, transform: [{ scaleY: -1 }] },
  olderHint: { ...type.caption, textAlign: "center", paddingTop: spacing.lg },
  headerTitle: { alignItems: "center" },
  // System font, not Source Sans 3 — see `type.navTitle`.
  headerName: type.navTitle,
  headerMeta: type.navMeta,
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
  olderButton: { minHeight: 44, alignItems: "center", justifyContent: "center", marginBottom: spacing.sm },
  olderText: { fontFamily: fonts.sansMedium, fontSize: 13, lineHeight: 18, color: colors.forest },
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
