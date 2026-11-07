import { useAuth } from "@clerk/expo";
import { SymbolView } from "../../../components/ui/SymbolView";
import {
  ApiError, buildRows, createApiClient, canControlSession, endedWhy, groupRows, mainEvents, pendingApproval, receiptChip, sessionLabel, sessionRingState, sessionStatus, sessionTitle, sessionViewKey, sessionScreenNotice, unavailableReason, useHistorySource, useLocalSessionHistory, usePendingPrompt, useSessionCommand, useSessionOutbox, useSessionResume, useWorkspaceSessions,
  type LocalSession, type SessionControlClient, type SessionSource, type TranscriptItem, sessionAgentName,
} from "@lightbulb/shared";
import { randomUUID } from "expo-crypto";
import { router, Stack, useIsFocused, useLocalSearchParams } from "expo-router";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ActivityIndicator, AppState, FlatList, Pressable, StyleSheet, Text, View,
  type NativeScrollEvent, type NativeSyntheticEvent,
} from "react-native";
import { KeyboardAvoidingView } from "react-native-keyboard-controller";
import { useSafeAreaInsets } from "react-native-safe-area-context";

import { TimelineSkeleton } from "../../../components/chat/TimelineSkeleton";
import { ConversationHeader } from "../../../components/conversation/ConversationHeader";
import { Avatar } from "../../../components/room/Avatar";
import { JoinRequests, WatchOnly } from "../../../components/session/SessionJoin";
import { SessionApprovalCard } from "../../../components/session/SessionApprovalCard";
import { SessionOverview } from "../../../components/session/SessionOverview";
import { ResumeDelivery, SessionResume } from "../../../components/session/SessionResume";
import { SessionPromptRow } from "../../../components/session/SessionPromptRow";
import { SessionRenameSheet } from "../../../components/session/SessionRenameSheet";
import { PendingPromptRow, TranscriptRowView, WorkingRow } from "../../../components/session/TranscriptRow";
import { BottomSheet } from "../../../components/ui/BottomSheet";
import { fonts, radii, spacing, useChatPalette } from "../../../config/theme";
import { markSessionsSeenOnDevice, memberName } from "../../../lib/localSessions";
import { makeAdapter } from "../../../lib/adapter";
import { JumpToLatest } from "../../../components/conversation/JumpToLatest";
import { usePushedScreenKeyboardOffset } from "../../../lib/keyboardOffset";
import { useReadBack } from "../../../lib/readBack";
import { relativeTime } from "../../../lib/relativeTime";
import { useMobileRooms } from "../../../lib/rooms";
import { fetchMembers, useTeamsConfig, type CompanyMember } from "../../../lib/teamsData";

/** Distance from the bottom, in px, still counted as "at the bottom" (the room screen's value). */
const BOTTOM_SLACK = 80;

/** The header's status line, as a sentence: "Working on your Mac", "Leo’s Mac is offline". */
function sessionWhere(session: LocalSession, own: boolean, ownerName: string): string {
  const mac = own ? "your Mac" : `${ownerName}’s Mac`;
  // A paused session is never heartbeated, so it reads offline whether or not its Mac is on.
  if (session.status === "offline" && session.shared === false) return `Paused on ${mac}`;
  if (session.status === "offline") return `${mac[0].toUpperCase()}${mac.slice(1)} is offline`;
  const why = endedWhy(session, mac, "’");
  if (why) return `Ended · ${why}`;
  if (session.status === "ended" && session.endedAt) return `Ended · ${relativeTime(Date.parse(session.endedAt))}`;
  return `${sessionStatus[session.status]} on ${mac}`;
}

/**
 * One shared Claude Code / Codex session, drawn in its TUI's grammar. Sessions
 * are workspace-scoped, not a room's: the session row always comes fresh from
 * the workspace session list (status, generation, control availability). The
 * prompt row goes to whoever may send (`canControlSession`): the sharer, the
 * team while the sharer has control on, and a teammate the sharer let in.
 * The body remounts when the session restarts, exactly as the web pane does.
 */
export default function SessionScreen() {
  const { sessionId } = useLocalSearchParams<{ sessionId: string }>();
  const { userId, getToken } = useAuth();
  const getTokenRef = useRef(getToken);
  getTokenRef.current = getToken;
  const analyticsApi = useMemo(() => createApiClient(makeAdapter(() => getTokenRef.current())), []);
  const openId = useRef(randomUUID());
  const reportedOpen = useRef<string | null>(null);
  const focused = useIsFocused();
  // Only a backgrounded app stops tailing — "inactive" (app switcher, a system
  // sheet) keeps it live. Both the initial state and the listener use this same rule.
  const [appActive, setAppActive] = useState(AppState.currentState !== "background");
  useEffect(() => {
    const subscription = AppState.addEventListener("change", (state) => setAppActive(state !== "background"));
    return () => subscription.remove();
  }, []);
  const live = focused && appActive;
  const palette = useChatPalette();
  // visible: false — this screen belongs to no single room, so it marks nothing read.
  const rooms = useMobileRooms({ subscribeToRoom: false, autoSelect: false, visible: false });
  const workspace = useWorkspaceSessions(rooms.resourceSource, { enabled: live });
  const listed = workspace.sessions.find((s) => s.sessionId === sessionId);
  // The workspace list excludes ended sessions -- one missing from it (once the
  // list has settled) is fetched directly, the same fallback the web pane uses,
  // so an ended session still opens with its real status.
  const coldLoading = workspace.loading && workspace.sessions.length === 0;
  const hasListedSession = Boolean(listed);
  const [retryRevision, setRetryRevision] = useState(0);
  const [fallback, setFallback] = useState<{ sessionId: string; session: LocalSession | null; notFound: boolean; error: boolean } | null>(null);
  const onSessionEvent = rooms.resourceSource.onEvent;
  const [metadataRevision, setMetadataRevision] = useState(0);
  useEffect(() => {
    if (!live) return;
    return onSessionEvent(event => {
      if (event.type === "workspace.sessions.changed" && !event.payload.transcript) setMetadataRevision(v => v + 1);
    });
  }, [onSessionEvent, live]);
  const revalidate = useRef<((hint: number, retry: number) => void) | null>(null);
  useEffect(() => {
    if (hasListedSession || !sessionId || !live) return;
    let alive = true;
    let pending = false;
    let queued = false;
    let attempts = 0;
    let lastRequested: { hint: number; retry: number } | null = null;
    let timer: ReturnType<typeof setTimeout> | undefined;
    // Keep the current request and its retry budget through same-session hints/list refreshes.
    // Coalesce those hints into one revalidation after this bounded sequence settles.
    const fetchSession = async () => {
      attempts += 1;
      try {
        const session = await rooms.resourceSource.client.getLocalSession(sessionId);
        if (alive) setFallback({ sessionId, session, notFound: false, error: false });
      } catch (e) {
        if (!alive) return;
        const notFound = e instanceof ApiError && e.status === 404;
        if (!notFound && attempts < 3) {
          timer = setTimeout(() => void fetchSession(), attempts * 1000);
          return;
        }
        setFallback(previous => ({ sessionId,
          session: !notFound && previous?.sessionId === sessionId ? previous.session : null,
          notFound, error: !notFound }));
      }
      if (!alive) return;
      pending = false;
      if (queued) { queued = false; request(); }
    };
    const request = () => {
      if (pending) { queued = true; return; }
      pending = true;
      attempts = 0;
      void fetchSession();
    };
    revalidate.current = (hint, retry) => {
      // Ordinary list polls do not restart a settled 404 or exhausted sequence.
      if (lastRequested?.hint === hint && lastRequested.retry === retry) return;
      lastRequested = { hint, retry };
      request();
    };
    return () => { alive = false; clearTimeout(timer); revalidate.current = null; };
  }, [hasListedSession, rooms.resourceSource.client, sessionId, live]);
  useEffect(() => {
    if (!hasListedSession && !coldLoading && sessionId && live) revalidate.current?.(metadataRevision, retryRevision);
  }, [hasListedSession, coldLoading, rooms.resourceSource.client, sessionId, metadataRevision, retryRevision, live]);
  const retryFallback = () => {
    setFallback(current => current?.session ? { ...current, error: false } : null);
    setRetryRevision(v => v + 1);
  };
  const resolvedFallback = fallback?.sessionId === sessionId ? fallback : null;
  const session = listed ?? resolvedFallback?.session ?? undefined;
  useEffect(() => {
    if (!session || reportedOpen.current === session.sessionId) return;
    reportedOpen.current = session.sessionId;
    void analyticsApi.post(`/messaging/sessions/${encodeURIComponent(session.sessionId)}/opened`, { openId: openId.current }).catch(() => {});
  }, [session?.sessionId, analyticsApi]);
  // Seen while open: the Shared sessions list's unread dot clears for what you watched.
  useEffect(() => { if (session) markSessionsSeenOnDevice([session], session.sessionId); }, [session]);
  const members = useMemo(() => rooms.rooms.flatMap((room) => room.members ?? []), [rooms.rooms]);
  // Workspace-wide, so a shared session's owner can be someone the viewer shares
  // no room with -- the org roster first (the same GET /teams/members
  // sessions.tsx uses), room members as the fallback.
  const config = useTeamsConfig();
  const [roster, setRoster] = useState<CompanyMember[]>([]);
  useEffect(() => {
    let alive = true;
    void fetchMembers(config).then((list) => { if (alive) setRoster(list); });
    return () => { alive = false; };
  }, [config]);
  const ownerName = useMemo(() => {
    if (!session) return "Workspace member";
    const member = roster.find((m) => m.userId === session.humanUserId);
    return member ? (member.name || member.email || "Workspace member") : memberName(members, session.humanUserId);
  }, [session, roster, members]);
  const own = session ? session.humanUserId === userId : false;
  // Anyone in the workspace can ask to join, so the roster names them; a room
  // they share with the reader is the fallback.
  const personName = useCallback((who: string) => {
    const member = roster.find((m) => m.userId === who);
    return member?.name || member?.email || members.find((m) => m.id === who)?.displayName || "A teammate";
  }, [roster, members]);
  const name = useCallback(
    (who: string | null) => (who ? members?.find((m) => m.id === who)?.displayName ?? "Teammate" : ownerName),
    [members, ownerName],
  );
  // A prompt row with no explicit actor is the session's own owner typing
  // directly at the terminal; one with an actor is mine exactly when it's me.
  const isMine = useCallback((who: string | null) => (who === null ? own : who === userId), [own, userId]);
  const messageOwner = useCallback(() => {
    if (!session) return;
    void rooms
      .selectDm({ kind: "human", id: session.humanUserId }, { activate: false })
      .then((dm) => { if (dm) router.push(`/(app)/room/${dm}`); })
      // Rejection is already surfaced via rooms.selectRoomError below;
      // this only stops it from becoming an unhandled rejection.
      .catch(() => {});
  }, [rooms, session]);
  const [chatError, setChatError] = useState<string | null>(null);
  const refreshSessions = workspace.refresh;
  // The session's chat (PR #3807): the room screen, pushed; back returns here.
  const openChat = useCallback(() => {
    if (!session) return;
    setChatError(null);
    rooms.resourceSource.client.openSessionChat(session.sessionId)
      // The list learns the room (a first open creates the seat), so its next message counts here.
      .then((room) => { void refreshSessions(); router.push(`/(app)/room/${room.roomId}`); })
      .catch(() => setChatError("Chat is not available for this session."));
  }, [rooms.resourceSource.client, session, refreshSessions]);
  // Hoisted above the early returns below — see the room screen.
  const keyboardOffset = usePushedScreenKeyboardOffset();
  const [detailsOpen, setDetailsOpen] = useState(false);
  const [renameOpen, setRenameOpen] = useState(false);

  const headerOptions = {
    // The back button is the (app) Stack's own: this is a pushed screen.
    title: session ? sessionTitle(session) : "Session",
    // Avatar (the tool's mark, ringed by its live state) + name + where it runs;
    // tapping opens the details sheet (today's summary lines and SessionOverview).
    headerTitle: session ? () => (
      <ConversationHeader
        // The tool is the avatar's mark, so the title is the session alone;
        // VoiceOver still hears the tool through `accessibilityTitle`.
        title={sessionTitle(session)}
        accessibilityTitle={sessionLabel(session)}
        avatar={<Avatar name={sessionLabel(session)} kind="agent" vendor={session.provider} ring={sessionRingState(session)} size={28} palette={palette} />}
        status={sessionWhere(session, own, ownerName)}
        statusTone={session.status === "busy" ? "live" : "mute"}
        palette={palette}
        onPress={() => setDetailsOpen(true)}
      />
    ) : undefined,
    // Chat for everyone who can read the session; the DM button only on someone else's.
    headerRight: session ? () => (
      <View style={styles.headerActions}>
        <Pressable accessibilityRole="button" accessibilityLabel={session.chatUnread ? `Chat, ${session.chatUnread} unread` : "Chat"}
          hitSlop={12} onPress={openChat} style={styles.chatButton}>
          <SymbolView accessibilityElementsHidden name="bubble.left.and.bubble.right" size={20} tintColor={palette.fgSoft} />
          {!!session.chatUnread && <Text style={[styles.chatCount, { color: palette.link }]}>{session.chatUnread > 99 ? "99+" : session.chatUnread}</Text>}
        </Pressable>
        {session.humanUserId !== userId && (
          <Pressable
            accessibilityRole="button"
            accessibilityLabel={`Message ${ownerName}`}
            // 20pt icon + 12pt hitSlop on every side reaches the 44pt tap-target floor.
            hitSlop={12}
            onPress={messageOwner}
          >
            <SymbolView accessibilityElementsHidden name="bubble.left" size={20} tintColor={palette.fgSoft} />
          </Pressable>
        )}
      </View>
    ) : undefined,
  };

  if (!session) {
    return (
      <View style={[styles.container, { backgroundColor: palette.ground }]}>
        <Stack.Screen options={headerOptions} />
        <View style={styles.centered}>
          {workspace.error ? (
            <>
              <Text accessibilityRole="alert" style={[styles.errorText, { color: palette.alert }]}>{workspace.error}</Text>
              <Pressable onPress={() => { retryFallback(); void workspace.refresh(); }} accessibilityRole="button" accessibilityLabel="Try again">
                <Text style={[styles.link, { color: palette.link }]}>Try again</Text>
              </Pressable>
            </>
          ) : resolvedFallback?.error ? (
            <>
              <Text accessibilityRole="alert" style={[styles.errorText, { color: palette.alert }]}>Could not load this session.</Text>
              <Pressable onPress={retryFallback} accessibilityRole="button" accessibilityLabel="Try again">
                <Text style={[styles.link, { color: palette.link }]}>Try again</Text>
              </Pressable>
            </>
          ) : resolvedFallback?.notFound ? (
            <Text style={[styles.meta, { color: palette.fgMute }]}>This session is no longer shared.</Text>
          ) : (
            <TimelineSkeleton />
          )}
        </View>
      </View>
    );
  }
  return (
    <View style={[styles.container, { backgroundColor: palette.ground }]}>
      <Stack.Screen options={headerOptions} />
      {!hasListedSession && resolvedFallback?.error && <View style={styles.banner}>
        <Text accessibilityRole="alert" style={[styles.errorText, { color: palette.alert }]}>Could not refresh this session.</Text>
        <Pressable onPress={retryFallback} accessibilityRole="button" accessibilityLabel="Try again">
          <Text style={[styles.link, { color: palette.link }]}>Try again</Text>
        </Pressable>
      </View>}
      {rooms.selectRoomError && (
        // A failed DM open (the header button) is transient and must not hide
        // the transcript underneath a full-screen blocked state -- see
        // sessions.tsx's identical inline treatment of selectRoomError.
        <View style={styles.banner}>
          <Text accessibilityRole="alert" style={[styles.errorText, { color: palette.alert }]}>{rooms.selectRoomError}</Text>
        </View>
      )}
      {chatError && <View style={styles.banner}>
        <Text accessibilityRole="alert" style={[styles.errorText, { color: palette.alert }]}>{chatError}</Text>
      </View>}
      <SessionBody
        key={sessionViewKey(session)}
        session={session}
        client={rooms.resourceSource.client}
        revision={workspace.hintRevision}
        onAccessLost={workspace.refresh}
        ownerName={ownerName}
        own={own}
        control={canControlSession(session, own, userId)}
        personName={personName}
        name={name}
        isMine={isMine}
        onMessageOwner={messageOwner}
        keyboardOffset={keyboardOffset}
        detailsOpen={detailsOpen}
        onOpenDetails={() => setDetailsOpen(true)}
        onCloseDetails={() => setDetailsOpen(false)}
        // Backstop poll while this screen is live AND the session is producing
        // output (same 5s cadence as web): transcript hints are throttled
        // server-side with no trailing edge. Idle sessions stay on hints plus
        // the sessions safety poll. Stopped when the screen blurs or the app
        // backgrounds.
        pollMs={live && session.status === "busy" ? 5000 : 0}
        onRename={() => { setDetailsOpen(false); setRenameOpen(true); }}
      />
      <SessionRenameSheet session={session} visible={renameOpen} onClose={() => setRenameOpen(false)}
        onSave={async (title) => {
          await rooms.resourceSource.client.setLocalSessionDisplayTitle(session.sessionId, title);
          await workspace.refresh();
        }} />
    </View>
  );
}

function SessionBody({
  session, client, revision, onAccessLost, ownerName, own, control, personName, name, isMine, onMessageOwner, keyboardOffset, detailsOpen, onOpenDetails, onCloseDetails, pollMs, onRename,
}: {
  session: LocalSession;
  client: SessionSource["client"] & SessionControlClient & Parameters<typeof JoinRequests>[0]["client"];
  revision: number;
  onAccessLost: () => void;
  ownerName: string;
  own: boolean;
  /** Whether the reader may send to this session. */
  control: boolean;
  personName: (userId: string) => string;
  name: (who: string | null) => string;
  isMine: (who: string | null) => boolean;
  onMessageOwner: () => void;
  keyboardOffset: number;
  detailsOpen: boolean;
  onOpenDetails: () => void;
  onCloseDetails: () => void;
  pollMs: number;
  onRename: () => void;
}) {
  const palette = useChatPalette();
  const insets = useSafeAreaInsets();
  const busy = session.status === "busy";
  const ended = session.status === "ended";
  // Running with no terminal, whatever the reason (or none reported): the web panel's condition.
  const noScreen = !session.terminal && session.status !== "ended" && session.status !== "offline";
  const source = useHistorySource(client, session, revision);
  const history = useLocalSessionHistory(client, source.sessionId, source.latestEventSeq, revision, onAccessLost, { pollMs });
  const [text, setText] = useState("");
  const command = useSessionCommand(client, session, { uuid: randomUUID, onAccessLost, onSent: () => setText(""), agentName: name, allowInterrupt: own || !!session.teamControl, allowOffline: true });
  const { userId } = useAuth();
  const resume = useSessionResume(client, session, { own, userId, ownerName, nameOf: (who) => personName(who), uuid: randomUUID });
  const outbox = useSessionOutbox(client, session.sessionId,
    `${session.status}:${session.status === "offline" ? revision : 0}:${command.attempt?.request.requestId}:${command.receipt?.state}`,
    session.status !== "offline" && session.status !== "ended");
  useEffect(() => {
    const updated = outbox.commands.find(receipt => receipt.requestId === command.attempt?.request.requestId);
    if (updated) command.syncReceipt(updated);
  }, [outbox.commands, command.attempt?.request.requestId, command.syncReceipt]);
  // The session's own events, for its pending prompt and wait; a subagent's are tagged with it.
  const events = useMemo(() => mainEvents(history.events), [history.events]);
  const replacingOldId = command.attempt?.state !== "rejected" && command.receipt?.state !== "rejected"
    ? command.attempt?.replacesRequestId : undefined;
  const savedOutbox = outbox.commands.filter(receipt => receipt.requestId !== command.attempt?.request.requestId && receipt.requestId !== replacingOldId);
  const queuedOutbox = savedOutbox.filter(receipt => receipt.state !== "applied");
  const pendingText = usePendingPrompt(command.attempt, events);
  const pendingShown = !!pendingText && !history.hasNewer;
  const waitingOn = session.status === "waiting-approval" ? pendingApproval(events) ?? session.waitingFor ?? "" : null;
  const remoteApproval = !!session.pendingApproval && (session.provider === "claude" || session.provider === "codex") && waitingOn !== null;
  const last = events.at(-1);
  const turnOpen = busy && !history.hasNewer && last?.lifecycle !== "turn_end" && last?.lifecycle !== "turn_aborted";
  // From every event: a subagent is drawn inline where it was launched and opens onto its own rows.
  const rows = useMemo(() => groupRows(buildRows(history.events), turnOpen, session.provider), [history.events, turnOpen, session.provider]);

  // Newest first: the list is inverted, the way every chat draws a timeline, so
  // the newest output is at offset 0 and simply appears there. Opening the
  // screen lands on it, and it stays in view as the session writes -- no
  // scroll-to-end to race the layout, nothing to tap to catch up.
  const newestFirst = useMemo(() => [...rows].reverse(), [rows]);
  const listRef = useRef<FlatList<TranscriptItem>>(null);
  const [atBottom, setAtBottom] = useState(true);
  const { loading, error, load } = history;
  const loadOlder = useCallback(() => void load("older"), [load]);
  const readBack = useReadBack({
    scope: session.sessionId,
    oldest: history.events[0]?.seq ?? 0,
    canLoad: !loading && !error,
    load: loadOlder,
  });
  const onScroll = useCallback((event: NativeSyntheticEvent<NativeScrollEvent>) => {
    setAtBottom(event.nativeEvent.contentOffset.y <= BOTTOM_SLACK);
    readBack.onScroll(event);
  }, [readBack]);
  const jumpToLatest = useCallback(() => {
    // Only after paging back so far that the window dropped its newest events.
    if (history.hasNewer) void history.load("latest");
    listRef.current?.scrollToOffset({ offset: 0, animated: true });
  }, [history]);

  return (
    <KeyboardAvoidingView
      style={[styles.container, { backgroundColor: palette.ground }]}
      behavior="padding"
      keyboardVerticalOffset={keyboardOffset}
    >
      {waitingOn !== null && !remoteApproval && (
        // Questions and Codex permission prompts cannot be answered remotely.
        // The full reason and the "Message owner" link are in Details.
        <View style={[styles.waitingBanner, { backgroundColor: palette.surface, borderColor: palette.alert }]} accessibilityLiveRegion="polite">
          <Text style={[styles.waitingBannerText, { color: palette.fg }]} numberOfLines={1}>
            {own ? "Waiting on you in your terminal" : `Waiting on ${ownerName} in their terminal`}
          </Text>
          <Pressable onPress={onOpenDetails} accessibilityRole="button" accessibilityLabel="Details" hitSlop={8}>
            <Text style={[styles.bannerLink, { color: palette.link }]}>Details</Text>
          </Pressable>
        </View>
      )}
      {noScreen && (
        // No terminal: it runs inside an app (this is its conversation), or its Mac app is an older one.
        <Text style={[styles.meta, styles.liveHint, { color: palette.fgMute }]} accessibilityLiveRegion="polite">
          {/* The web panel's words (sessionScreenNotice), on one line. */}
          {(({ title, body }) => `${title} ${body}`)(sessionScreenNotice(session, own, ownerName))}
        </Text>
      )}
      {remoteApproval && <SessionApprovalCard key={session.pendingApproval?.approvalId} session={session} client={client} allowed={control} own={own} ownerName={ownerName} />}
      {own && <JoinRequests session={session} client={client} personName={personName} onAnswered={onAccessLost} />}
      <View style={styles.timeline}>
        <FlatList
          ref={listRef}
          testID="session-transcript"
          inverted
          data={newestFirst}
          keyExtractor={(row) => String(row.seq)}
          // Holds the reader's place when output arrives while they are scrolled
          // back, and keeps the newest in view when they are at the bottom.
          maintainVisibleContentPosition={{ minIndexForVisible: 0, autoscrollToTopThreshold: BOTTOM_SLACK }}
          contentContainerStyle={[
            styles.list,
            rows.length === 0 && styles.listEmpty,
            // An ended session has nothing under its transcript, so the newest
            // output is what has to clear the home indicator.
            ended && { paddingTop: spacing.lg + insets.bottom },
          ]}
          keyboardDismissMode="interactive"
          keyboardShouldPersistTaps="handled"
          onScroll={onScroll}
          scrollEventThrottle={16}
          onLayout={readBack.onLayout}
          onContentSizeChange={readBack.onContentSizeChange}
          // Header and footer are swapped against an un-inverted list: the HEADER
          // draws at the bottom, under the newest output.
          ListHeaderComponent={
            <View>
              {turnOpen && <WorkingRow provider={session.provider} since={last?.ts} />}
              {pendingShown && <PendingPromptRow provider={session.provider} text={pendingText} ownerName={ownerName} chip={command.chip} />}
              {queuedOutbox.map(receipt => <View key={receipt.requestId}>
                <PendingPromptRow provider={session.provider} text={receipt.text ?? ""} ownerName={ownerName}
                  label="Follow-up delivery"
                  chip={receiptChip({ request: { requestId: receipt.requestId, expectedGeneration: receipt.expectedGeneration, kind: "send", text: receipt.text ?? "" }, state: "received", receipt }, name)} />
                {receipt.state === "rejected" && !!receipt.text && <View style={styles.actions}>
                  <Text accessibilityRole="alert" style={[styles.errorText, { color: palette.alert }]}>
                    {receipt.cancelledAt ? "Cancelled." : receipt.deliverBy <= Date.now() / 1000 ? "This follow-up expired before your Mac reconnected." : "The session rejected this follow-up."}
                  </Text>
                  {!receipt.cancelledAt && <Pressable onPress={() => command.send(receipt.text!, false, receipt.requestId)} disabled={command.locked || !command.canSend} accessibilityRole="button" accessibilityLabel="Retry follow-up" accessibilityState={{ disabled: command.locked || !command.canSend }}>
                    <Text style={[styles.link, { color: palette.link }]}>Retry</Text>
                  </Pressable>}
                </View>}
                {outbox.canCancel && receipt.state === "pending" && <Pressable onPress={() => void outbox.cancel(receipt.requestId)} accessibilityRole="button" accessibilityLabel={`Cancel queued follow-up: ${(receipt.text ?? "").slice(0, 40)}`}>
                  <Text style={[styles.link, { color: palette.alert }]}>Cancel</Text>
                </Pressable>}
              </View>)}
              {queuedOutbox.some(receipt => receipt.state === "pending" || receipt.state === "dispatched" || receipt.state === "unknown") && (
                <Pressable onPress={() => void outbox.refresh()} accessibilityRole="button" accessibilityLabel="Check queued delivery">
                  <Text style={[styles.link, { color: palette.link }]}>Check queued delivery</Text>
                </Pressable>
              )}
            </View>
          }
          ListFooterComponent={
            <View>
              {history.loading && history.hasOlder && <ActivityIndicator style={styles.loadingOlder} color={palette.fgMute} />}
              {history.error && (
                <View style={styles.errorCard}>
                  <Text accessibilityRole="alert" style={[styles.errorText, { color: palette.alert }]}>{history.error}</Text>
                  <Pressable onPress={() => void history.retry()} accessibilityRole="button" accessibilityLabel="Retry history">
                    <Text style={[styles.link, { color: palette.link }]}>Retry</Text>
                  </Pressable>
                </View>
              )}
            </View>
          }
          // Upright inside the flipped list (see the room screen's identical wrapper).
          ListEmptyComponent={
            <View style={styles.emptyUpright}>
              {history.loading ? <TimelineSkeleton /> : history.error ? null : (
                <Text style={[styles.meta, { color: palette.fgMute }]}>
                  {session.provider === "claude" && ended && !session.controlUnavailableReason ? "This session ended before anything was sent." : "No shared output yet."}
                </Text>
              )}
            </View>
          }
          renderItem={({ item, index }) => (
            <TranscriptRowView provider={session.provider} row={item} name={name} isMine={isMine} live={turnOpen && index === 0} events={history.events} status={session.status} />
          )}
        />
        {/* Also when the window no longer holds the tail (the reader paged back
            past what it keeps): at the bottom of THAT window this is the only
            way to the session's newest output. */}
        {(!atBottom || history.hasNewer) && <JumpToLatest palette={palette} onPress={jumpToLatest} />}
      </View>
      {!ended && <ResumeDelivery flow={resume} />}
      {ended ? <SessionResume flow={resume} own={own} ownerName={ownerName} />
        : control ? <SessionPromptRow session={session} text={text} onChangeText={setText} command={command} pendingShown={pendingShown} />
        : <WatchOnly ownerName={ownerName} />}
      <BottomSheet visible={detailsOpen} onClose={onCloseDetails} large>
        <View style={styles.sheetContent}>
          <View style={styles.sheetTextGroup}>
            <Text style={[styles.sheetHeadline, { color: palette.fg }]}>
              {`${ownerName} · ${sessionStatus[session.status]}${session.cwdName ? ` · ${session.cwdName}` : ""}`}
            </Text>
            <Text style={[styles.sheetMeta, { color: palette.fgSoft }]}>
              {`${session.providerVersion} · Follow-ups ${session.capabilities.sendMode === "queue" ? "queue" : "wait"} on ${own ? "your" : `${ownerName}’s`} Mac. ${session.provider === "claude" ? "Exact Claude permissions can be answered here; questions stay in the terminal." : session.provider === "codex" ? "Codex permissions and first-run folder trust stay in the terminal." : `${sessionAgentName(session.provider)} permissions stay in the terminal.`}${session.workspace?.overlap ? " Multiple sessions may overlap this checkout." : ""}`}
            </Text>
            {session.controlUnavailableReason && (
              <Text style={[styles.sheetMeta, { color: palette.warn }]}>{unavailableReason[session.controlUnavailableReason]}</Text>
            )}
            {own && <Pressable accessibilityRole="button" accessibilityLabel="Rename session" onPress={onRename}>
              <Text style={[styles.link, { color: palette.link }]}>Rename session</Text>
            </Pressable>}
            {waitingOn !== null && !remoteApproval && (
              <View style={styles.waitingBlock} accessibilityLiveRegion="polite">
                <Text style={[styles.waitingTitle, { color: palette.fg }]}>{own ? "Waiting on you in your terminal" : `Waiting on ${ownerName} in their terminal`}</Text>
                {waitingOn ? <Text style={[styles.waitingReason, { color: palette.fgSoft }]}>{waitingOn}</Text> : null}
                {own ? <Text style={[styles.sheetMeta, { color: palette.fgSoft }]}>{session.provider === "claude" ? "Claude questions and prompts without an exact remote approval must be answered on your Mac." : session.provider === "codex" ? "Codex permissions and first-run folder trust must be answered on your Mac." : `${sessionAgentName(session.provider)} permissions must be answered on your Mac.`}</Text> : (
                  <Pressable onPress={onMessageOwner} accessibilityRole="button" accessibilityLabel={`Message ${ownerName}`}>
                    <Text style={[styles.bannerLink, { color: palette.link }]}>Message {ownerName}</Text>
                  </Pressable>
                )}
              </View>
            )}
          </View>
          <SessionOverview events={events} palette={palette} />
        </View>
      </BottomSheet>
    </KeyboardAvoidingView>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1 },
  headerActions: { flexDirection: "row", alignItems: "center", gap: 20 },
  chatButton: { flexDirection: "row", alignItems: "center", gap: 4 },
  chatCount: { fontFamily: fonts.sansSemiBold, fontSize: 13, lineHeight: 18 },
  centered: { flex: 1, padding: spacing.lg, gap: spacing.sm },
  // Top/bottom are swapped against an un-inverted list: `paddingTop` draws at
  // the bottom (against the composer), `paddingBottom` at the top.
  list: { paddingHorizontal: spacing.md, paddingTop: spacing.lg, paddingBottom: spacing.sm },
  listEmpty: { flexGrow: 1 },
  emptyUpright: { flexGrow: 1, transform: [{ scaleY: -1 }] },
  meta: { fontFamily: fonts.sans, fontSize: 12, lineHeight: 16 },
  // Full horizontal padding -- unlike errorCard below, this sits directly on
  // the screen, not inside the transcript's already-padded list.
  banner: { padding: spacing.lg, gap: spacing.xs },
  waitingBanner: {
    marginHorizontal: spacing.lg,
    marginTop: spacing.sm,
    padding: spacing.sm,
    borderWidth: 1,
    borderRadius: radii.lg,
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    gap: spacing.sm,
  },
  liveHint: { marginHorizontal: spacing.lg, marginTop: spacing.sm },
  waitingBannerText: { flex: 1, fontFamily: fonts.sansSemiBold, fontSize: 14, lineHeight: 18 },
  waitingBlock: { gap: 2, marginTop: spacing.xs },
  waitingTitle: { fontFamily: fonts.sansSemiBold, fontSize: 14, lineHeight: 18 },
  waitingReason: { fontFamily: fonts.mono, fontSize: 12, lineHeight: 16 },
  errorCard: { paddingVertical: spacing.sm, gap: spacing.xs },
  errorText: { fontFamily: fonts.sans, fontSize: 13, lineHeight: 18 },
  actions: { flexDirection: "row", alignItems: "center", flexWrap: "wrap", gap: spacing.sm },
  link: { fontFamily: fonts.sansSemiBold, fontSize: 13, lineHeight: 18, paddingVertical: spacing.xs },
  bannerLink: { fontFamily: fonts.sansSemiBold, fontSize: 13, lineHeight: 18 },
  timeline: { flex: 1 },
  loadingOlder: { paddingVertical: spacing.sm },
  sheetContent: { gap: spacing.md, paddingBottom: spacing.lg },
  sheetTextGroup: { paddingHorizontal: spacing.lg, gap: 4 },
  sheetHeadline: { fontFamily: fonts.sansMedium, fontSize: 15, lineHeight: 20 },
  sheetMeta: { fontFamily: fonts.sans, fontSize: 13, lineHeight: 18 },
});
