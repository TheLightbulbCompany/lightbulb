import { useAuth } from "@clerk/clerk-expo";
import { SymbolView } from "expo-symbols";
import {
  ApiError, canControlSession, mainEvents, pendingApproval, receiptChip, sessionLabel, sessionRingState, sessionStatus, sessionTitle, sessionViewKey, terminalStateReason, unavailableReason, useHistorySource, useLocalSessionHistory, usePendingPrompt, useSessionCommand, useSessionOutbox, useSessionResume, useWorkspaceSessions,
  type LocalSession, type SessionControlClient, type SessionSource,
} from "@lightbulb/shared";
import { randomUUID } from "expo-crypto";
import { router, Stack, useIsFocused, useLocalSearchParams } from "expo-router";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AppState, Pressable, StyleSheet, Text, View } from "react-native";
import { KeyboardAvoidingView } from "react-native-keyboard-controller";
import { useSafeAreaInsets } from "react-native-safe-area-context";

import { TimelineSkeleton } from "../../../components/chat/TimelineSkeleton";
import { ConversationHeader } from "../../../components/conversation/ConversationHeader";
import { Avatar } from "../../../components/room/Avatar";
import { AskToJoin, JoinRequests } from "../../../components/session/SessionJoin";
import { SessionApprovalCard } from "../../../components/session/SessionApprovalCard";
import { SessionOverview } from "../../../components/session/SessionOverview";
import { ResumeDelivery, SessionResume } from "../../../components/session/SessionResume";
import { SessionPromptRow } from "../../../components/session/SessionPromptRow";
import { SessionRenameSheet } from "../../../components/session/SessionRenameSheet";
import { SessionTerminal } from "../../../components/session/SessionTerminal";
import { PendingPromptRow } from "../../../components/session/TranscriptRow";
import { BottomSheet } from "../../../components/ui/BottomSheet";
import { fonts, radii, spacing, useChatPalette } from "../../../config/theme";
import { markSessionsSeenOnDevice, memberName } from "../../../lib/localSessions";
import { useDesktopSessionApi } from "../../../lib/desktopSessions";
import { usePushedScreenKeyboardOffset } from "../../../lib/keyboardOffset";
import { relativeTime } from "../../../lib/relativeTime";
import { useMobileRooms } from "../../../lib/rooms";
import { fetchMembers, useTeamsConfig, type CompanyMember } from "../../../lib/teamsData";

/** Older history pages read per session for Details: 20 pages is 1,000 events, under the history
 *  window's 2,000-row cap (the web panel's value). */
const MAX_OLDER_PAGES = 20;
/** How long an ended held session may be without its kept screen before the screen counts as gone:
 *  the Mac reports it on a later heartbeat (11.5 s in prod; the web panel's value). */
const SCREEN_DUE_MS = 20_000;

/** The header's status line, as a sentence: "Working on your Mac", "Leo’s Mac is offline". */
function sessionWhere(session: LocalSession, own: boolean, ownerName: string): string {
  const mac = own ? "your Mac" : `${ownerName}’s Mac`;
  if (session.status === "offline") return `${mac[0].toUpperCase()}${mac.slice(1)} is offline`;
  if (session.status === "ended" && session.endedAt) return `Ended · ${relativeTime(Date.parse(session.endedAt))}`;
  return `${sessionStatus[session.status]} on ${mac}`;
}

/**
 * One shared Claude Code / Codex session: its real terminal, live, or a plain
 * panel that says why there is none. Never a transcript. Sessions
 * are workspace-scoped, not a room's: the session row always comes fresh from
 * the workspace session list (status, generation, control availability). The
 * prompt row goes to whoever may send (`canControlSession`): the sharer, the
 * team while the sharer has control on, and a teammate the sharer let in.
 * The body remounts when the session restarts, exactly as the web pane does.
 */
export default function SessionScreen() {
  const { sessionId } = useLocalSearchParams<{ sessionId: string }>();
  const { userId } = useAuth();
  const analyticsApi = useDesktopSessionApi();
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
        onEvent={onSessionEvent}
        active={live}
        revision={workspace.hintRevision}
        onAccessLost={workspace.refresh}
        ownerName={ownerName}
        own={own}
        control={canControlSession(session, own, userId)}
        personName={personName}
        name={name}
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
  session, client, onEvent, active, revision, onAccessLost, ownerName, own, control, personName, name, onMessageOwner, keyboardOffset, detailsOpen, onOpenDetails, onCloseDetails, pollMs, onRename,
}: {
  session: LocalSession;
  client: SessionSource["client"] & SessionControlClient & Parameters<typeof JoinRequests>[0]["client"] & {
    watchLocalSession: (sessionId: string, fresh?: boolean) => Promise<unknown>;
    sendLocalSessionInput: (sessionId: string, data: string, epoch: number) => Promise<unknown>;
  };
  /** Live events: the terminal's frames arrive on them. */
  onEvent: SessionSource["onEvent"];
  /** The screen is focused and the app is not in the background: only then is the terminal watched. */
  active: boolean;
  revision: number;
  onAccessLost: () => void;
  ownerName: string;
  own: boolean;
  /** Whether the reader may send to this session. */
  control: boolean;
  personName: (userId: string) => string;
  name: (who: string | null) => string;
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
  const ended = session.status === "ended";
  const offline = session.status === "offline";
  const reason = terminalStateReason(session);
  // The history feeds the pending prompt, the approval reason and Details. No rows are drawn from it.
  const source = useHistorySource(client, session, revision);
  const history = useLocalSessionHistory(client, source.sessionId, source.latestEventSeq, revision, onAccessLost, { pollMs });
  // Nothing scrolls the history any more, so read back for Details: bounded, as on the web.
  const olderPages = useRef(0);
  useEffect(() => {
    if (!history.hasOlder || history.loading || history.error || olderPages.current >= MAX_OLDER_PAGES) return;
    olderPages.current += 1;
    void history.load("older");
  }, [history]);
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
  const waitingOn = session.status === "waiting-approval" ? pendingApproval(events) ?? "" : null;
  const remoteApproval = !!session.pendingApproval && session.provider === "claude" && waitingOn !== null;

  // The same rules as the web panel (LocalSessionPanel.tsx): a live terminal where the Mac has one
  // (held, or a Claude background session), the kept final screen of an ended held session while
  // its owner's app runs, and otherwise a plain panel that says why there is none. Never a transcript.
  const hasTerminal = !!session.terminal;
  const running = !ended && !offline;
  const endedScreen = ended && session.screenAvailable === true && hasTerminal;
  // Claude's SessionEnd hook ends the session before the Mac reports the kept screen: until that is
  // due the panel does not say the screen is gone, and the timer redraws it when the wait is over.
  const screenDueAt = ended && (hasTerminal || session.wasHeld === true) && !endedScreen && session.endedAt ? Date.parse(session.endedAt) + SCREEN_DUE_MS : 0;
  const [, redraw] = useState(0);
  useEffect(() => {
    const left = screenDueAt - Date.now();
    if (!(left > 0)) return;
    const timer = setTimeout(() => redraw(n => n + 1), left);
    return () => clearTimeout(timer);
  }, [screenDueAt]);
  const screenDue = Date.now() < screenDueAt;
  // A viewer who was watching when the session ended keeps that terminal, with its last picture,
  // until the kept screen is reported or the wait is over.
  const watched = useRef(false);
  const keptWatching = screenDue && watched.current;
  const watchRemote = (hasTerminal && (running || endedScreen)) || keptWatching;
  useEffect(() => { watched.current = watchRemote; }, [watchRemote]);
  const [screenLive, setScreenLive] = useState(false);
  // False once the Mac refused input: the follow-up row is the way to send then.
  const [termCanType, setTermCanType] = useState(true);
  const liveTerminal = watchRemote && screenLive;
  const notStarted = running && !hasTerminal;
  const admitted = running && control && session.shared !== false && !(session.inputPaused && !own);
  const paused = liveTerminal && control && !own && !!session.inputPaused;
  // A live terminal a person can type into is the only input: no row under it.
  const typesInTerminal = liveTerminal && control && (termCanType || paused);
  const sessionId = session.sessionId;
  const input = useCallback((data: string, epoch: number) => client.sendLocalSessionInput(sessionId, data, epoch), [client, sessionId]);
  const endedEmpty = session.provider === "claude" && session.latestEventSeq === 0 && !session.controlUnavailableReason;
  const mac = own ? "your" : `${ownerName}’s`;

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
      {remoteApproval && <SessionApprovalCard key={session.pendingApproval?.approvalId} session={session} client={client} allowed={control} own={own} ownerName={ownerName} />}
      {own && <JoinRequests session={session} client={client} personName={personName} onAnswered={onAccessLost} />}
      {history.error && (
        <View style={styles.banner}>
          <Text accessibilityRole="alert" style={[styles.errorText, { color: palette.alert }]}>{history.error}</Text>
          <Pressable onPress={() => void history.retry()} accessibilityRole="button" accessibilityLabel="Retry history">
            <Text style={[styles.link, { color: palette.link }]}>Retry</Text>
          </Pressable>
        </View>
      )}
      {watchRemote ? (
        <SessionTerminal session={session} onEvent={onEvent} watch={client.watchLocalSession} active={active} kept={endedScreen}
          onLive={setScreenLive} onCanType={setTermCanType} admitted={admitted} input={input}
          // Under a terminal a person types into there is nothing else to clear the home indicator.
          bottomInset={typesInTerminal && !paused ? insets.bottom : 0} />
      ) : (
        <View style={styles.state} accessibilityLiveRegion="polite">
          {ended ? <>
            <Text style={[styles.stateTitle, { color: palette.fg }]}>
              {screenDue && !endedEmpty ? "Ended just now" : session.endedAt ? `Ended ${relativeTime(Date.parse(session.endedAt))}` : "Ended"}
            </Text>
            {(endedEmpty || !screenDue) && <Text style={[styles.stateText, { color: palette.fgSoft }]}>
              {endedEmpty ? "This session ended before anything was sent." : "The screen is gone: this session’s terminal closed."}
            </Text>}
          </> : offline ? <>
            <Text style={[styles.stateTitle, { color: palette.fg }]}>{own ? "Your Mac is offline" : `${ownerName}’s Mac is offline`}</Text>
            {session.lastEventAt && <Text style={[styles.stateText, { color: palette.fgSoft }]}>Last seen {relativeTime(Date.parse(session.lastEventAt))}</Text>}
            <Text style={[styles.stateText, { color: palette.fgSoft }]}>{`Resume works once ${mac} Mac is back.`}</Text>
          </> : notStarted ? <>
            <Text style={[styles.stateTitle, { color: palette.fg }]}>This session was not started through Lightbulb, so its screen is not here.</Text>
            {reason !== null && <Text style={[styles.stateText, { color: palette.fgSoft }]}>
              {reason === "plain"
                ? own ? "Press ← in its terminal to share this one live, or turn on Share every session live in Settings to make every new session live." : `Ask ${ownerName} to press ← in their terminal.`
                : own ? "Start it again with lightbulb codex to share its screen." : `Ask ${ownerName} to start it again with lightbulb codex.`}
            </Text>}
          </> : null}
        </View>
      )}
      {/* The sent-but-not-echoed prompt and the queued follow-ups: delivery, not history. */}
      {(pendingShown || queuedOutbox.length > 0) && (
        <View style={styles.feedback}>
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
      )}
      {paused && <Text style={[styles.meta, styles.liveHint, { color: palette.fgMute, paddingBottom: Math.max(insets.bottom, spacing.md) }]}>Paused while {ownerName} answers a request to join.</Text>}
      {!ended && <ResumeDelivery flow={resume} />}
      {ended ? <SessionResume flow={resume} own={own} ownerName={ownerName} />
        : typesInTerminal ? null
        : control ? <SessionPromptRow session={session} text={text} onChangeText={setText} command={command} pendingShown={pendingShown} />
        : <AskToJoin session={session} client={client} ownerName={ownerName} />}
      <BottomSheet visible={detailsOpen} onClose={onCloseDetails} large>
        <View style={styles.sheetContent}>
          <View style={styles.sheetTextGroup}>
            <Text style={[styles.sheetHeadline, { color: palette.fg }]}>
              {`${ownerName} · ${sessionStatus[session.status]}${session.cwdName ? ` · ${session.cwdName}` : ""}`}
            </Text>
            <Text style={[styles.sheetMeta, { color: palette.fgSoft }]}>
              {`${session.providerVersion} · Follow-ups ${session.capabilities.sendMode === "queue" ? "queue" : "wait"} on ${own ? "your" : `${ownerName}’s`} Mac. ${session.provider === "claude" ? "Exact Claude permissions can be answered here; questions stay in the terminal." : "Codex permissions and first-run folder trust stay in the terminal."}${session.workspace?.overlap ? " Multiple sessions may overlap this checkout." : ""}`}
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
                {own ? <Text style={[styles.sheetMeta, { color: palette.fgSoft }]}>{session.provider === "claude" ? "Claude questions and prompts without an exact remote approval must be answered on your Mac." : "Codex permissions and first-run folder trust must be answered on your Mac."}</Text> : (
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
  state: { flex: 1, padding: spacing.lg, gap: spacing.sm },
  stateTitle: { fontFamily: fonts.sansSemiBold, fontSize: 16, lineHeight: 22 },
  stateText: { fontFamily: fonts.sans, fontSize: 14, lineHeight: 20 },
  feedback: { paddingHorizontal: spacing.md, paddingVertical: spacing.sm },
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
  sheetContent: { gap: spacing.md, paddingBottom: spacing.lg },
  sheetTextGroup: { paddingHorizontal: spacing.lg, gap: 4 },
  sheetHeadline: { fontFamily: fonts.sansMedium, fontSize: 15, lineHeight: 20 },
  sheetMeta: { fontFamily: fonts.sans, fontSize: 13, lineHeight: 18 },
});
