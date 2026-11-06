import { useAuth } from "@clerk/expo";
import {
  buildTimeline,
  cardAnswersFor,
  lastOwnMessageId,
  roomDisplayName,
  placeRuns,
  readState,
  readStateLabel,
  runView,
  sessionMentionCandidates,
  useWorkspaceSessions,
  type CanonicalRoomMessage,
} from "@lightbulb/shared";
import { Stack, useFocusEffect, useLocalSearchParams } from "expo-router";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { FlatList, Pressable, StyleSheet, Text, View } from "react-native";
import { KeyboardAvoidingView } from "react-native-keyboard-controller";

import { TimelineSkeleton } from "../../../../../components/chat/TimelineSkeleton";
import { ConversationHeader } from "../../../../../components/conversation/ConversationHeader";
import { AVATAR_GUTTER, MetaText, WorkingBubble } from "../../../../../components/conversation/bubbles";
import { SavedDelegations } from "../../../../../components/room/DelegateCard";
import { MessageActionsSheet } from "../../../../../components/room/MessageActionsSheet";
import { LocalMessageRows, MessageGroup } from "../../../../../components/room/MessageGroup";
import { RoomBlockedState } from "../../../../../components/room/RoomBlockedState";
import { RoomComposer } from "../../../../../components/room/RoomComposer";
import { RunBlock } from "../../../../../components/room/RunBlock";
import { fonts, spacing, useChatPalette, type } from "../../../../../config/theme";
import { claimRoomTarget } from "../../../../../lib/activeAgent";
import { useDraftKey } from "../../../../../lib/composerDraft";
import { memberName } from "../../../../../lib/localSessions";
import { usePushedScreenKeyboardOffset } from "../../../../../lib/keyboardOffset";
import { cardControls } from "../../../../../lib/cardControls";
import { cardReplySender, useMobileRooms, useSendDrafts, useTypingReport } from "../../../../../lib/rooms";
import { typingLine } from "../../../../../lib/typingLine";

/**
 * One thread: its root pinned above the list, its replies below in the same item
 * renderer the room uses, and a composer that sends back into it.
 *
 * Slack's "Also send to #channel" toggle is out of scope this round (spec).
 */
export default function ThreadScreen() {
  const { roomId, messageId } = useLocalSearchParams<{ roomId: string; messageId: string }>();
  const { userId } = useAuth();
  const palette = useChatPalette();
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
  const rooms = useMobileRooms({ subscribeToRoom: focused, autoSelect: false, visible: false, boundedHistory: true });
  const workspaceSessions = useWorkspaceSessions(rooms.resourceSource, { enabled: focused && !!rooms.activeRoom && !rooms.activeRoom.shared, includeEnded: true });
  // Each thread drafts on its own, separately from the room's timeline.
  const composerDraftKey = useDraftKey(roomId, messageId ?? null);

  // selectRoom's identity changes with every message (it closes over
  // markVisibleRead), and each call bumps the hook's selection-cancellation
  // counter — so fire it on roomId only, through a ref.
  const selectRoomRef = useRef(rooms.selectRoom);
  selectRoomRef.current = rooms.selectRoom;
  useEffect(() => {
    if (roomId) selectRoomRef.current(roomId);
  }, [roomId]);

  const { activeRoom, messages, historyLoading, historyComplete, historyError, loadHistory } = rooms;
  const root = messages.find((message) => message.messageId === messageId) ?? null;
  // An old thread root may be outside the newest room page. Its replies are
  // newer than the root, so finding that root once also finds every reply.
  const fullHistoryRequested = useRef<string | null>(null);
  useEffect(() => {
    if (!roomId || !messageId || activeRoom?.roomId !== roomId || !messages.length || root || historyLoading || historyComplete || historyError) return;
    const target = `${roomId}:${messageId}`;
    if (fullHistoryRequested.current === target) return;
    fullHistoryRequested.current = target;
    loadHistory();
  }, [roomId, messageId, activeRoom, messages.length, root, historyLoading, historyComplete, historyError, loadHistory]);
  const replies = useMemo(
    () => rooms.messages.filter((message) => message.threadRootMessageId === messageId),
    [rooms.messages, messageId],
  );
  // readSeq is null on purpose: a thread has no "New messages" line in Slack,
  // and the room's own read cursor counts the room's sequence, not this thread's.
  const items = useMemo(() => buildTimeline(replies, { readSeq: null }), [replies]);
  // Reversed for the inverted list below — see the room screen's own note.
  const newestFirst = useMemo(() => [...items].reverse(), [items]);
  const runs = useMemo(
    () => rooms.runs.filter((run) => run.threadRootMessageId === messageId).map(runView),
    [rooms.runs, messageId],
  );
  // A live run is the tail under the newest message; an ended one (stopped, failed, no reply)
  // stays after the message it followed, so a later message never renders above it.
  const placed = useMemo(() => placeRuns(runs, replies), [runs, replies]);

  // A bubble's time waits for its run's last bubble: whose successor (oldest →
  // newest) is a collapsed message from the same author, so its own time never
  // shows — same computation as the room screen, scoped to this thread's `items`.
  const noTimeIds = useMemo(() => {
    const ids = new Set<string>();
    for (let index = 0; index < items.length - 1; index += 1) {
      const current = items[index];
      const next = items[index + 1];
      if (current.kind === "message" && next.kind === "message" && next.collapsed) {
        ids.add(current.message.messageId);
      }
    }
    return ids;
  }, [items]);
  // The viewer's own last REPLY carries the read state — Task 8's pattern,
  // scoped to this thread's own messages (`replies`) rather than the room's.
  const lastOwnId = useMemo(() => lastOwnMessageId(replies, userId ?? ""), [replies, userId]);

  const members = rooms.activeRoom?.members;
  const sessionCandidates = useMemo(() => rooms.activeRoom?.shared ? [] : sessionMentionCandidates(
    workspaceSessions.sessions.filter((session) => session.kind !== "background"),
  ), [rooms.activeRoom?.shared, workspaceSessions.sessions]);
  // A chip is a reference, not a send: offer every shared session whose owner is in the room (#3623).
  const sessionTargets = useMemo(() => sessionCandidates
    .filter(({ session }) => session.shared !== false
      && members?.some((member) => member.kind === "human" && member.id === session.humanUserId))
    .map((candidate) => ({
    candidate, ownerName: candidate.session.humanUserId === userId ? "You" : memberName(members, candidate.session.humanUserId),
  })), [sessionCandidates, members, userId]);
  const viewer = members?.find((member) => member.kind === "human" && member.id === userId);
  const others = (members ?? []).filter(
    (member) => !(member.kind === "human" && member.id === userId),
  );
  const subtitle = roomDisplayName(rooms.activeRoom, others);

  const { canSend, send, retry, rows } = useSendDrafts(rooms, {
    authorId: userId ?? "",
    authorName: viewer?.displayName ?? "You",
    threadRootMessageId: messageId ?? null,
  });
  const { cardsEnabled, repliesEnabled } = cardControls(rooms.activeRoom);
  const onCardReply = useMemo(() => cardReplySender(send), [send]);
  const cardAnswers = useMemo(() => cardAnswersFor(rooms.messages), [rooms.messages]);
  const reportTyping = useTypingReport(rooms.setTyping, focused);
  const typingText = typingLine(rooms.typingMembers.filter((member) => member.userId !== userId));

  // The message a long press is acting on; dropping it starts the sheet's
  // close animation. `lastActionTarget` keeps the sheet's content stable
  // through that close — see the room screen's identical pattern.
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
    (targetMessageId: string, emoji: string, active: boolean) => {
      setReactionErrorMessageId(null);
      void rooms
        .setReaction(targetMessageId, emoji, active)
        .catch(() => setReactionErrorMessageId(targetMessageId));
    },
    [rooms],
  );

  const runningRun = runs.find((run) => run.state === "running");
  const agentName = useCallback(
    (agentId: string) =>
      members?.find((member) => member.kind === "agent" && member.id === agentId)?.displayName ??
      "Agent",
    [members],
  );

  // Hoisted above the blocked-state early return below — see the room screen.
  const keyboardOffset = usePushedScreenKeyboardOffset();

  // The back button still belongs to the (app) Stack (its `anchor` puts the
  // tabs beneath it on a cold start into a deep-linked thread). The title area
  // is this screen's own, same split as the room screen: a nested
  // `Stack.Screen` overrides `headerTitle` to draw the shared
  // `ConversationHeader` — title "Thread", the room name as its status line, no
  // avatar (a thread has no single subject) and no `onPress` (no thread
  // "details" screen to open).
  const headerOptions = {
    contentStyle: { backgroundColor: palette.ground },
    headerTitle: () => (
      <ConversationHeader title="Thread" status={subtitle} avatar={null} palette={palette} />
    ),
  };

  // Same guard as the room screen (search `room-blocked` in web's RoomPanel):
  // a reader with no access, or a room that failed to open, replaces the
  // WHOLE panel, not just this thread's timeline/composer.
  if (rooms.roomLoading || rooms.accessRemoved || rooms.roomError || rooms.selectRoomError) {
    const blockedState = rooms.roomLoading
      ? "loading" as const
      : rooms.accessRemoved
        ? "access-removed" as const
        : "unavailable" as const;
    return (
      <View style={[styles.container, { backgroundColor: palette.ground }]}>
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
      style={[styles.container, { backgroundColor: palette.ground }]}
      behavior="padding"
      keyboardVerticalOffset={keyboardOffset}
    >
      <Stack.Screen options={headerOptions} />
      <FlatList
        testID="thread-timeline"
        // Inverted, like the room's timeline: a new reply or run frame appears
        // at the bottom with no scroll animation. Header and footer swap with
        // it — the header draws at the BOTTOM of an inverted list.
        inverted
        data={newestFirst}
        keyExtractor={(item) => item.key}
        contentContainerStyle={styles.list}
        keyboardDismissMode="interactive"
        keyboardShouldPersistTaps="handled"
        ListHeaderComponent={
          <View>
            <LocalMessageRows
              rows={rows}
              onRetry={(clientMessageId) => void retry(clientMessageId)}
              attachmentUrl={rooms.attachmentUrl}
              members={members ?? []}
              currentUserId={userId ?? ""}
            />
            {placed.tail.map((run) => (
              <RunBlock
                key={run.runId}
                run={run}
                name={agentName(run.agentId)}
                onAbort={() => rooms.abortRun(run.runId)}
                onSteer={(text) => rooms.steerRun(run.runId, text)}
              />
            ))}
            {typingText ? (
              <View style={styles.typingWrap}>
                <WorkingBubble palette={palette} accessibilityLabel={typingText} />
              </View>
            ) : null}
          </View>
        }
        // The thread ROOT, pinned above its replies — so, on an inverted list,
        // the footer.
        ListFooterComponent={
          root ? (
            <View>
              <MessageGroup
                message={root}
                collapsed={false}
                replyCount={0}
                currentUserId={userId ?? ""}
                reactionError={reactionErrorMessageId === root.messageId}
                onToggleReaction={(emoji, active) => reactToMessage(root.messageId, emoji, active)}
                onLongPress={() => setActionTarget(root)}
                attachmentUrl={rooms.attachmentUrl}
                members={members ?? []}
                sessionCandidates={sessionCandidates}
                cardsEnabled={cardsEnabled}
                onCardReply={repliesEnabled && canSend ? onCardReply : undefined}
                cardAnswer={cardAnswers.get(root.messageId) ?? null}
              />
              <View style={[styles.hairline, { backgroundColor: palette.edge }]} />
              {replies.length > 0 && (
                <Text style={[styles.count, { color: palette.fgMute }]}>
                  {replies.length} {replies.length === 1 ? "reply" : "replies"}
                </Text>
              )}
            </View>
          ) : rooms.historyLoading ? (
            <TimelineSkeleton />
          ) : (
            <View style={styles.missing}>
              <Text style={[styles.missingText, { color: palette.fgMute }]}>Couldn’t load this thread.</Text>
              <Pressable
                onPress={rooms.loadHistory}
                accessibilityRole="button"
                accessibilityLabel="Retry loading history"
              >
                <Text style={[styles.retryText, { color: palette.link }]}>Retry</Text>
              </Pressable>
            </View>
          )
        }
        renderItem={({ item }) => {
          if (item.kind === "date") {
            return (
              <View style={styles.dateDivider}>
                <MetaText palette={palette}>{item.label}</MetaText>
              </View>
            );
          }
          // Unreachable while readSeq is null; buildTimeline's union still has it.
          if (item.kind === "new") return null;
          return (
            <View>
              <MessageGroup
                message={item.message}
                collapsed={item.collapsed}
                replyCount={0}
                currentUserId={userId ?? ""}
                showTime={!noTimeIds.has(item.message.messageId)}
                readLabel={
                  rows.length === 0 && item.message.messageId === lastOwnId
                    ? readStateLabel(
                        readState({
                          message: item.message,
                          roomKind: rooms.activeRoom?.kind ?? "channel",
                          messages: replies,
                          receipts: rooms.receipts,
                          members: members ?? [],
                          viewerId: userId ?? "",
                          // A room-wide human read cursor proves nothing about this thread — see readState's doc.
                          ignoreHumanCursors: true,
                        }),
                      )
                    : null
                }
                reactionError={reactionErrorMessageId === item.message.messageId}
                onToggleReaction={(emoji, active) =>
                  reactToMessage(item.message.messageId, emoji, active)
                }
                onLongPress={() => setActionTarget(item.message)}
                attachmentUrl={rooms.attachmentUrl}
                members={members ?? []}
                sessionCandidates={sessionCandidates}
                cardsEnabled={cardsEnabled}
                onCardReply={repliesEnabled && canSend ? onCardReply : undefined}
                cardAnswer={cardAnswers.get(item.message.messageId) ?? null}
              />
              <SavedDelegations state={rooms.subagents} entry={rooms.delegationsByMessage.get(item.message.messageId)} />
              {placed.after.get(item.message.messageId)?.map((run) => (
                <RunBlock
                  key={run.runId}
                  run={run}
                  name={agentName(run.agentId)}
                  onAbort={() => rooms.abortRun(run.runId)}
                  onSteer={(text) => rooms.steerRun(run.runId, text)}
                />
              ))}
            </View>
          );
        }}
      />
      {(() => {
        // Local const so TS narrows it through the closure below — see the
        // room screen's identical pattern for why `actionTarget` itself must
        // not be read inside it.
        const sheetMessage = lastActionTarget.current;
        if (!sheetMessage) return null;
        return (
          <MessageActionsSheet
            message={sheetMessage}
            visible={actionTarget !== null}
            currentUserId={userId ?? ""}
            roomId={roomId}
            onClose={() => setActionTarget(null)}
            // Reaction failures surface inline under the message (see
            // `reactionErrorMessageId` above), matching the web's `reactionError`.
            onReact={(emoji, active) => reactToMessage(sheetMessage.messageId, emoji, active)}
            // No Reply in thread and no Mark unread — the web's `!inThread` rule.
            // A reply cannot root a nested thread, and the read cursor is per room,
            // so marking unread here is undone the moment the conversation behind
            // this screen is re-focused; the action stays thread-hidden until
            // per-thread read state exists (Phase B, needs a backend field).
          />
        );
      })()}
      <RoomComposer
        members={members ?? []}
        sessionTargets={sessionTargets}
        placeholder="Reply…"
        disabled={!canSend}
        reconnecting={!rooms.liveConnected}
        onSend={send}
        onUploadAttachment={rooms.uploadAttachment}
        onAbort={runningRun ? () => void rooms.abortRun(runningRun.runId).catch(() => {}) : null}
        onTypingChange={reportTyping}
        draftKey={composerDraftKey}
        roomKind={rooms.activeRoom?.kind}
      />
    </KeyboardAvoidingView>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1 },
  // `paddingTop` draws at the bottom on an inverted list — see the room screen.
  list: { paddingHorizontal: spacing.lg, paddingTop: spacing.xl },
  // Indented level with a run's avatar column (AVATAR_GUTTER), last child of
  // the inverted list's header so it draws under the newest reply — see the
  // room screen's identical placement (F13).
  typingWrap: { marginLeft: AVATAR_GUTTER, marginTop: spacing.sm },
  hairline: {
    height: StyleSheet.hairlineWidth,
    marginTop: spacing.md,
  },
  count: { ...type.caption, marginTop: spacing.sm },
  // A date divider carries no rules — just the centred label (matches the
  // room screen's treatment).
  dateDivider: { alignItems: "center", marginTop: spacing.md, marginBottom: spacing.xs },
  missing: { paddingTop: spacing.xl, gap: spacing.sm },
  missingText: { fontFamily: fonts.sans, fontSize: 14, lineHeight: 20 },
  retryText: { fontFamily: fonts.sansMedium, fontSize: 13, lineHeight: 18 },
});
