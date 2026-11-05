import { useAuth } from "@clerk/expo";
import { SymbolView } from "../../../../components/ui/SymbolView";
import {
  buildTimeline,
  cardAnswersFor,
  lastOwnMessageId,
  liveMarker,
  roomDisplayName,
  placeRuns,
  readState,
  readStateLabel,
  runView,
  sessionMentionCandidates,
  SPEAKING_AGENT,
  useWorkspaceSessions,
  type CanonicalRoomMessage,
  type MemberPrefsRequest,
  type TimelineItem,
} from "@lightbulb/shared";
import { router, Stack, useFocusEffect, useIsPreview, useLocalSearchParams } from "expo-router";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ActivityIndicator,
  FlatList,
  Pressable,
  StyleSheet,
  Text,
  useWindowDimensions,
  View,
  type NativeScrollEvent,
  type NativeSyntheticEvent,
} from "react-native";
import { KeyboardAvoidingView } from "react-native-keyboard-controller";

import { EmptyState } from "../../../../components/chat/EmptyState";
import { TimelineSkeleton } from "../../../../components/chat/TimelineSkeleton";
import { ConversationHeader } from "../../../../components/conversation/ConversationHeader";
import { AVATAR_GUTTER, MetaText, WorkingBubble } from "../../../../components/conversation/bubbles";
import { Avatar } from "../../../../components/room/Avatar";
import { CatchUpSummary } from "../../../../components/room/CatchUpSummary";
import { SavedDelegations } from "../../../../components/room/DelegateCard";
import { IdeasSheet } from "../../../../components/room/IdeasSheet";
import { MessageActionsSheet } from "../../../../components/room/MessageActionsSheet";
import { LocalMessageRows, MessageGroup } from "../../../../components/room/MessageGroup";
import { NoticeBanner } from "../../../../components/voice/NoticeBanner";
import { OnboardingBand } from "../../../../components/room/OnboardingBand";
import { RoomBlockedState } from "../../../../components/room/RoomBlockedState";
import { RoomComposer } from "../../../../components/room/RoomComposer";
import { RoomHeaderActions } from "../../../../components/room/RoomHeaderActions";
import { RoomSubagentStrip } from "../../../../components/room/RoomSubagentStrip";
import { RunBlock } from "../../../../components/room/RunBlock";
import { fonts, radii, spacing, useChatPalette } from "../../../../config/theme";
import { claimRoomTarget } from "../../../../lib/activeAgent";
import { hasNativeVoice } from "../../../../lib/webrtc";
import { useLiveEvents, useSuperAgent } from "../../../../providers/live-events";
import { useContainerStatusContext } from "../../../../lib/containerStatusContext";
import { useLive, useRoomLive } from "../../../../providers/live";
import { JumpToLatest } from "../../../../components/conversation/JumpToLatest";
import { usePushedScreenKeyboardOffset } from "../../../../lib/keyboardOffset";
import { useReadBack } from "../../../../lib/readBack";
import { useDraftKey } from "../../../../lib/composerDraft";
import { memberName } from "../../../../lib/localSessions";
import { usePresence } from "../../../../lib/presence";
import { cardControls } from "../../../../lib/cardControls";
import { cardReplySender, useMobileRooms, useSendDrafts, useTypingReport } from "../../../../lib/rooms";
import { typingLine } from "../../../../lib/typingLine";

/** RoomHeader.tsx's own copy — the same two strings the room list shows. */
const STAR_FAILED = "The star could not be saved.";
const MUTE_FAILED = "The notification setting could not be saved.";

/**
 * How far the reader may scroll back, in px, before the jump-to-latest pill
 * appears. The list is INVERTED, so `contentOffset.y === 0` is the newest
 * message and the offset grows going back in time — no content/layout
 * arithmetic, and nothing to recompute when the keyboard resizes the list.
 *
 * The same number the list follows new content within: at two different
 * numbers, a message arriving between them was neither followed nor counted
 * as missed.
 */
const JUMP_THRESHOLD = 48;
// What the navigation bar spends beside the title and the actions row: the
// back button and its margin (76), the actions' glass capsule padding and
// trailing margin (24), and a gap before the capsule (8). Measured on iOS 26.
const HEADER_SIDE_CHROME = 108;

export default function ConversationRoute() {
  const { roomId } = useLocalSearchParams<{ roomId: string }>();
  // Remount on a room change: the shared machine keys its history, runs and
  // read cursor off the mounted instance.
  return <Conversation key={roomId ?? "none"} />;
}

function Conversation() {
  const { roomId } = useLocalSearchParams<{ roomId: string }>();
  const { userId } = useAuth();
  const palette = useChatPalette();
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
  // `subagents`: the focused screen shows the strip, so only it snapshots and polls (a peek stays read-only) (the shared hook
  // also refreshes on return to the foreground while the strip is visible).
  const rooms = useMobileRooms({ subscribeToRoom: focused, autoSelect: false, visible: focused, boundedHistory: true, subagents: focused });
  const { messaging, frontAgentId: frontId } = useLiveEvents();
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
  const workspaceSessions = useWorkspaceSessions(rooms.resourceSource, { enabled: focused && !preview && !!room && !room.shared, includeEnded: true });

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
  // Sending is the reader saying they are caught up: the line goes, as it does on
  // the web. Without this it was drawn above the reader's own message, since
  // that is the first one past where they had read to.
  const [sentSinceOpen, setSentSinceOpen] = useState(false);
  const items = useMemo(
    () => buildTimeline(mainMessages, { readSeq: sentSinceOpen ? null : readSeq }),
    [mainMessages, readSeq, sentSinceOpen],
  );
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
  // A live run is the tail under the newest message; an ended one (stopped, failed, no reply)
  // stays after the message it followed, so a later message never renders above it.
  const placed = useMemo(() => placeRuns(runs, mainMessages), [runs, mainMessages]);

  // A bubble's time waits for its run's last bubble: whose successor (oldest →
  // newest) is a collapsed message from the same author, so its own time never shows.
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
  const lastOwnId = useMemo(() => lastOwnMessageId(mainMessages, userId ?? ""), [mainMessages, userId]);
  // The avatar ring on a message a still-running run already posted -- only its
  // NEWEST message, so an earlier reply from the same run doesn't also ring.
  const runningAgentRingIds = useMemo(() => {
    const ids = new Set<string>();
    for (const run of runs) {
      if (run.state !== "running") continue;
      let newest: CanonicalRoomMessage | null = null;
      for (const message of mainMessages) {
        if (message.runId === run.runId && (!newest || message.messageSeq > newest.messageSeq)) newest = message;
      }
      if (newest) ids.add(newest.messageId);
    }
    return ids;
  }, [runs, mainMessages]);

  const members = room?.members;
  const sessionCandidates = useMemo(() => room?.shared ? [] : sessionMentionCandidates(
    workspaceSessions.sessions.filter((session) => session.kind !== "background"),
  ), [room?.shared, workspaceSessions.sessions]);
  // A chip is a reference, not a send: offer every shared session whose owner is in the room (#3623).
  const sessionTargets = useMemo(() => sessionCandidates
    .filter(({ session }) => session.shared !== false
      && members?.some((member) => member.kind === "human" && member.id === session.humanUserId))
    .map((candidate) => ({
    candidate, ownerName: candidate.session.humanUserId === userId ? "You" : memberName(members, candidate.session.humanUserId),
  })), [sessionCandidates, members, userId]);
  const viewer = members?.find((member) => member.kind === "human" && member.id === userId);
  // Super-agent: the front agent reads "Lightbulb" whatever its stored seat name ("CEO").
  const superAgentOn = useSuperAgent();
  const others = (members ?? []).filter(
    (member) => !(member.kind === "human" && member.id === userId),
  ).map((member) => (superAgentOn && member.kind === "agent" && member.id === frontId ? { ...member, displayName: "Lightbulb" } : member));
  // Super-agent, a new workspace still being set up: the viewer's latest message, with nothing after it, is admitted
  // and kept until the runtime is up (the Router waits), so it reads "Queued" rather than looking ignored.
  const containerStatus = useContainerStatusContext();
  const queuedLabel = superAgentOn && containerStatus?.kind === "starting" && others.some((m) => m.kind === "agent")
    && mainMessages[mainMessages.length - 1]?.messageId === lastOwnId
    ? "Queued · Lightbulb answers as soon as setup finishes" : null;
  const isChannel = room?.kind === "channel";
  const isDm = room?.kind === "dm";
  const isGroupDm = room?.kind === "group-dm";
  const canQuickAddPeople = isChannel
    && !room?.shared
    && (viewer?.role === "owner" || viewer?.role === "admin");
  const dmWithAgent = isDm && others[0]?.kind === "agent";
  const isHumanDm = isDm && others[0]?.kind === "human";
  // Super-agent: a DM with a specialist (any agent but the front one) says the specialist now works through
  // Lightbulb. Only once the front agent is known, so an unresolved roster never calls every agent a specialist.
  const frontAgentId = superAgentOn ? frontId : undefined;
  const specialistDm = !!frontAgentId && dmWithAgent && others.length === 1 && others[0]?.id !== frontAgentId;
  // One heartbeat/snapshot loop per screen that shows it (ponytail) — Chats
  // already runs its own, and only a human DM's status line reads this one.
  const presence = usePresence(focused && isHumanDm);
  const roomName = roomDisplayName(room, others);
  // ponytail: Phase A's RoomSummary carries no private flag, so every channel
  // gets `#`; swap in a lock glyph once the backend ships one.
  const title = `${isChannel ? "#" : ""}${roomName}`;
  const otherAgentWorking = others[0]?.kind === "agent" && runs.some((run) => run.state === "running" && run.agentId === others[0]?.id);
  const headerAvatar = isChannel ? (
    <View style={[styles.channelTile, { backgroundColor: palette.selected }]}>
      <Text style={[styles.channelHash, { color: palette.fgSoft }]} maxFontSizeMultiplier={1}>
        #
      </Text>
    </View>
  ) : (
    <Avatar
      name={others[0]?.displayName ?? "Conversation"}
      kind={others[0]?.kind ?? "human"}
      size={28}
      palette={palette}
      ring={isDm && otherAgentWorking ? "running" : null}
      showPresence={isDm}
      online={isDm && !!others[0] && presence.has(others[0].id)}
    />
  );
  // Live in every room kind (the web's RoomHeader rules); a room with no listening agent is people-only.
  const live = useLive();
  const roomLive = useRoomLive(!preview && room && !room.guest ? room.roomId : undefined);
  const canVoice = hasNativeVoice();
  const liveNames = roomLive.userIds.map((id) => members?.find((m) => m.kind === "human" && m.id === id)?.displayName ?? "Someone");
  // Only this room's Live names a speaker; the worker's attribute is a roster id.
  const speakingId = live.roomId === room?.roomId ? live.workerAttrs[SPEAKING_AGENT] : undefined;
  const speakerName = speakingId ? live.roster.find((m) => m.id === speakingId)?.displayName : undefined;
  const speaking = speakerName ? { name: speakerName } : undefined;
  const headerVoice = !canVoice || !room ? undefined
    // While Live, the call button reopens the sheet from any room, so Live can always be found and left.
    : live.room
      ? { active: true, speaking, label: live.roomId === room.roomId ? (speaking ? `Open Live, ${speaking.name} speaking` : "Open Live") : `Open Live in ${live.roomName ?? "another room"}`, onPress: () => live.setOpen(true) }
    : roomLive.available && !room.guest
      ? { label: "Go live", disabled: !!live.joining,
          onPress: () => void live.join(room.roomId, title) }
    : undefined;
  const headerStatus = liveNames.length > 0 ? liveMarker(liveNames) : isDm
    ? others[0]?.kind === "agent"
      ? otherAgentWorking
        ? "working…"
        : "idle"
      : others[0] && presence.has(others[0].id)
        ? "online"
        : null
    : isChannel
      ? `${members?.length ?? 0} ${members?.length === 1 ? "member" : "members"}${room?.shared ? " · Shared with Lightbulb" : ""}`
      : isGroupDm
        ? `${members?.length ?? 0} people`
        : null;
  const headerStatusTone: "mute" | "live" = isDm && otherAgentWorking ? "live" : "mute";

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
  const sendAndCatchUp = useCallback<typeof send>((message, options) => {
    setSentSinceOpen(true);
    return send(message, options);
  }, [send]);
  const { cardsEnabled, repliesEnabled } = cardControls(room);
  const onCardReply = useMemo(() => cardReplySender(sendAndCatchUp), [sendAndCatchUp]);
  const cardAnswers = useMemo(() => cardAnswersFor(rooms.messages), [rooms.messages]);
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
  // The newest message there was when the reader left the bottom: what has
  // arrived since, from anyone else, is the count on the way back.
  const newestSeq = mainMessages.at(-1)?.messageSeq ?? 0;
  const [leftAtSeq, setLeftAtSeq] = useState<number | null>(null);
  const unseen = leftAtSeq === null ? 0 : mainMessages.filter(
    (message) => message.messageSeq > leftAtSeq && !(message.author.kind === "human" && message.author.id === userId),
  ).length;
  // Older history loads as the reader approaches it, the way every messenger
  // does it; nothing to tap. Sequence 1 is the first message a room ever had,
  // so a room already holding it has nothing older whatever the flag says.
  const oldestSeq = rooms.messages[0]?.messageSeq ?? 0;
  const hasOlder = !rooms.historyComplete && oldestSeq > 1;
  const readBack = useReadBack({
    scope: roomId ?? "",
    oldest: oldestSeq,
    canLoad: hasOlder && !rooms.historyLoading && !rooms.historyError,
    load: rooms.loadOlder,
  });
  const onScroll = useCallback((event: NativeSyntheticEvent<NativeScrollEvent>) => {
    const bottom = event.nativeEvent.contentOffset.y <= JUMP_THRESHOLD;
    setAtBottom(bottom);
    setLeftAtSeq((current) => (bottom ? null : current ?? newestSeq));
    readBack.onScroll(event);
  }, [readBack, newestSeq]);

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
  // The native title view takes its content's width, so the title is bounded
  // to what the actions leave, or a long name runs underneath them.
  const { width: windowWidth } = useWindowDimensions();
  const [headerActionsWidth, setHeaderActionsWidth] = useState(72);
  const [ideasOpen, setIdeasOpen] = useState(false);

  // On `headerOptions` rather than inline so the blocked-state early return
  // below keeps the header too — a blocked screen with no title and no
  // star/mute is worse than the state it is reporting. The back button is the
  // (app) Stack's own: the room is a PUSHED screen now, not a Stack root.
  const headerOptions = {
    title,
    contentStyle: { backgroundColor: palette.ground },
    // Tapping the title opens the room's details, as in Slack.
    headerTitle: () => (
      <ConversationHeader
        title={roomName}
        status={headerStatus}
        statusTone={headerStatusTone}
        statusDot={liveNames.length > 0}
        avatar={headerAvatar}
        palette={palette}
        maxWidth={windowWidth - HEADER_SIDE_CHROME - headerActionsWidth}
        onPress={roomId ? () => router.push(`/(app)/room/${roomId}/details`) : undefined}
      />
    ),
    headerRight: () => (
      <RoomHeaderActions
        starred={!!room?.starred}
        muted={!!room?.muted}
        disabled={!room}
        onToggleStar={() => void savePrefs({ starred: !room?.starred }, STAR_FAILED)}
        onToggleMute={() => void savePrefs({ muted: !room?.muted }, MUTE_FAILED)}
        onAddPeople={canQuickAddPeople && roomId
          ? () => router.push(`/(app)/room/${roomId}/details?addPeople=1`)
          : undefined}
        voice={headerVoice}
        onIdeas={room ? () => setIdeasOpen(true) : undefined}
        onLayout={(event) => setHeaderActionsWidth(event.nativeEvent.layout.width)}
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
      <View style={[styles.container, { backgroundColor: palette.ground }]}>
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
      style={[styles.container, { backgroundColor: palette.ground }]}
      behavior="padding"
      keyboardVerticalOffset={keyboardOffset}
    >
      {!preview && <Stack.Screen options={headerOptions} />}
      <IdeasSheet
        visible={ideasOpen}
        onClose={() => setIdeasOpen(false)}
        agentId={dmWithAgent ? others[0]?.id : undefined}
        onStarted={(startedRoomId) => {
          setIdeasOpen(false);
          if (startedRoomId !== roomId) router.push(`/(app)/room/${startedRoomId}`);
        }}
      />
      {specialistDm ? (
        <Text accessibilityRole="text" testID="specialist-dm-banner" style={[styles.specialistBanner, { color: palette.fgMute, borderColor: palette.edge }]}>
          {roomName} now works through Lightbulb. A message here starts a {roomName} thread.
        </Text>
      ) : null}
      {/* The sheet shows its own copy; this one is for a hidden sheet (UI §8). */}
      {!preview && room && live.roomId === room.roomId && !live.open ? <NoticeBanner style={styles.liveNotice} /> : null}
      {prefsError && (
        <Text accessibilityRole="alert" style={[styles.prefsError, { color: palette.alert }]}>
          {prefsError}
        </Text>
      )}
      <CatchUpSummary
        messages={rooms.messages}
        readSeq={readSeq}
        offline={!rooms.liveConnected}
      />
      {/* The timeline and the way back to its newest message, which sits at the
          timeline's own bottom edge rather than a guessed height over the composer. */}
      <View style={styles.timeline}>
        <FlatList
          ref={listRef}
          testID="room-timeline"
          // Inverted, the way every chat app draws a timeline: the newest content
          // is at offset 0 and simply appears there, so a new message, a run frame
          // or the keyboard resizing the list never animates a scroll.
          inverted
          data={newestFirst}
          keyExtractor={(item) => item.key}
          maintainVisibleContentPosition={{ minIndexForVisible: 0, autoscrollToTopThreshold: JUMP_THRESHOLD }}
          contentContainerStyle={[styles.list, items.length === 0 && styles.listEmpty]}
          keyboardDismissMode="interactive"
          keyboardShouldPersistTaps="handled"
          onScroll={onScroll}
          onLayout={readBack.onLayout}
          onContentSizeChange={readBack.onContentSizeChange}
          scrollEventThrottle={16}
          // Header and footer are swapped against the un-inverted version: on an
          // inverted list the HEADER draws at the bottom, under the newest message.
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
                  subagents={rooms.subagents}
                />
              ))}
              {typingText ? (
                <View style={styles.typingWrap}>
                  <WorkingBubble palette={palette} accessibilityLabel={typingText} />
                </View>
              ) : null}
            </View>
          }
          // Upright inside the flipped list: the flip that rights every row is
          // handed to the row through its own `style` prop, which a component
          // that takes no `style` never applies — so the empty state drew upside
          // down. This wrapper takes the flip instead.
          ListEmptyComponent={
            <View style={styles.emptyUpright}>
              {/* Loading, or the newest page drew nothing (all thread replies) and
                  older history is being read back to find something that does. */}
              {rooms.historyLoading || hasOlder ? (
                <TimelineSkeleton />
              ) : (
                <EmptyState agentName={title} subtitle={dmWithAgent ? undefined : "No messages yet"} palette={palette} />
              )}
            </View>
          }
          ListFooterComponent={
            <View>
            {rooms.historyError ? (
              <View style={[styles.errorCard, { backgroundColor: palette.raised, borderColor: palette.alert }]}>
                <Text style={[styles.errorText, { color: palette.alert }]}>{rooms.historyErrorMessage ? `Couldn't load history: ${rooms.historyErrorMessage}` : "Couldn't load history."}</Text>
                <Pressable
                  onPress={rooms.loadHistory}
                  accessibilityRole="button"
                  accessibilityLabel="Retry loading history"
                >
                  <Text style={[styles.retryText, { color: palette.link }]}>Retry</Text>
                </Pressable>
              </View>
            ) : null}
            {hasOlder && rooms.historyLoading && items.length > 0 ? (
              <ActivityIndicator style={styles.olderLoading} color={palette.fgMute} accessibilityLabel="Loading older messages" />
            ) : null}
            {/* The web band's `leading` slot: above the oldest loaded message. */}
            {!preview && room?.isDefault ? <OnboardingBand participants={members ?? []} /> : null}
            </View>
          }
          renderItem={({ item }) => {
            if (item.kind === "date") {
              return (
                <View style={styles.dateDivider}>
                  <MetaText palette={palette}>{item.label}</MetaText>
                </View>
              );
            }
            if (item.kind === "new") {
              return (
                <View style={styles.divider}>
                  <View style={[styles.rule, { backgroundColor: palette.link }]} />
                  <Text style={[styles.newLabel, { color: palette.link }]}>New messages</Text>
                  <View style={[styles.rule, { backgroundColor: palette.link }]} />
                </View>
              );
            }
            return (
              <View>
                <MessageGroup
                  message={item.message}
                  collapsed={item.collapsed}
                  replyCount={replyCounts.get(item.message.messageId) ?? 0}
                  currentUserId={userId ?? ""}
                  showTime={!noTimeIds.has(item.message.messageId)}
                  readLabel={
                    rows.length === 0 && item.message.messageId === lastOwnId
                      ? queuedLabel ?? readStateLabel(
                          readState({
                            message: item.message,
                            roomKind: room?.kind ?? "channel",
                            messages: mainMessages,
                            receipts: rooms.receipts,
                            members: members ?? [],
                            viewerId: userId ?? "",
                          }),
                        )
                      : null
                  }
                  agentRing={runningAgentRingIds.has(item.message.messageId) ? "running" : null}
                  reactionError={reactionErrorMessageId === item.message.messageId}
                  onToggleReaction={(emoji, active) =>
                    reactToMessage(item.message.messageId, emoji, active)
                  }
                  onLongPress={() => setActionTarget(item.message)}
                  onOpenThread={() =>
                    router.push(`/(app)/room/${roomId}/thread/${item.message.messageId}`)
                  }
                  attachmentUrl={rooms.attachmentUrl}
                  members={members ?? []}
                  sessionCandidates={sessionCandidates}
                  cardsEnabled={cardsEnabled}
                  onCardReply={repliesEnabled && !preview && canSend ? onCardReply : undefined}
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
                    subagents={rooms.subagents}
                  />
                ))}
              </View>
            );
          }}
        />
        {!atBottom && (
          <JumpToLatest
            palette={palette}
            count={unseen}
            // Offset 0 IS the newest message on an inverted list.
            onPress={() => listRef.current?.scrollToOffset({ offset: 0, animated: true })}
          />
        )}
      </View>
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
      {room?.archivedAt ? (
        // An archived room takes no posts (the server refuses them): say so instead of a composer.
        <Text accessibilityRole="text" style={[styles.archived, { color: palette.fgSoft }]}>
          {room.session ? "This session ended. Its chat is read-only." : "This channel is archived."}
        </Text>
      ) : (
        <>
        <RoomSubagentStrip
          state={rooms.subagents}
          roomId={roomId ?? ""}
          agentName={agentName}
          client={messaging}
          onSteer={rooms.steerSubagent}
          onStop={rooms.stopSubagent}
          onVisible={rooms.subagentsPollVisible}
        />
        <RoomComposer
          members={members ?? []}
          sessionTargets={sessionTargets}
          placeholder={`Message ${title}`}
          disabled={!canSend}
          reconnecting={!rooms.liveConnected}
          onSend={sendAndCatchUp}
          onUploadAttachment={rooms.uploadAttachment}
          onAbort={runningRun ? () => void rooms.abortRun(runningRun.runId).catch(() => {}) : null}
          onTypingChange={reportTyping}
          draftKey={composerDraftKey}
          roomKind={room?.kind}
          runningAgentIds={runningAgentIds}
        />
        </>
      )}
    </KeyboardAvoidingView>
  );
}

const styles = StyleSheet.create({
  specialistBanner: {
    fontFamily: fonts.sans, fontSize: 13, lineHeight: 18, paddingHorizontal: spacing.lg, paddingVertical: spacing.sm,
    borderBottomWidth: StyleSheet.hairlineWidth,
  },
  container: { flex: 1 },
  liveNotice: { marginHorizontal: 12, marginTop: 8 },
  // Top/bottom are swapped against the un-inverted version: the content
  // container is inside the flipped scroll view, so `paddingTop` draws at the
  // bottom (against the composer) and `paddingBottom` at the top.
  list: { paddingHorizontal: spacing.md, paddingTop: spacing.lg, paddingBottom: spacing.sm },
  listEmpty: { flexGrow: 1 },
  emptyUpright: { flexGrow: 1, transform: [{ scaleY: -1 }] },
  // The channel avatar's "#" tile — 28pt, radius 8 (Global Constraints).
  channelTile: { width: 28, height: 28, borderRadius: 8, alignItems: "center", justifyContent: "center" },
  channelHash: { fontFamily: fonts.sansSemiBold, fontSize: 14, lineHeight: 18 },
  // Indented level with a run's avatar column (AVATAR_GUTTER), last child of
  // the inverted list's header so it draws under the newest content (F13).
  typingWrap: { marginLeft: AVATAR_GUTTER, marginTop: spacing.sm },
  prefsError: {
    fontFamily: fonts.sans,
    fontSize: 13,
    lineHeight: 18,
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
  // A date divider carries no rules — just the centred label.
  dateDivider: { alignItems: "center", marginTop: spacing.md, marginBottom: spacing.xs },
  rule: { flex: 1, height: StyleSheet.hairlineWidth },
  newLabel: {
    fontFamily: fonts.sansSemiBold,
    fontSize: 12,
    lineHeight: 16,
  },
  errorCard: {
    borderWidth: 1,
    borderRadius: radii.md,
    padding: spacing.lg,
    marginBottom: spacing.lg,
    gap: spacing.sm,
  },
  errorText: { fontFamily: fonts.sans, fontSize: 14, lineHeight: 20 },
  archived: { fontFamily: fonts.sans, fontSize: 14, lineHeight: 20, textAlign: "center", padding: spacing.md },
  retryText: { fontFamily: fonts.sansMedium, fontSize: 13, lineHeight: 18 },
  olderLoading: { marginVertical: spacing.md },
  timeline: { flex: 1 },
});
