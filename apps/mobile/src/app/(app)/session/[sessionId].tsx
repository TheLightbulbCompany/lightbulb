import { useAuth } from "@clerk/clerk-expo";
import { SymbolView } from "expo-symbols";
import {
  ApiError, buildRows, groupRows, mainEvents, pendingApproval, sessionLabel, sessionRingState, sessionStatus, sessionTitle, sessionViewKey, unavailableReason, useLocalSessionHistory, usePendingPrompt, useSessionCommand, useWorkspaceSessions,
  type LocalSession, type SessionSource, type TranscriptItem,
} from "@lightbulb/shared";
import { randomUUID } from "expo-crypto";
import { router, Stack, useIsFocused, useLocalSearchParams } from "expo-router";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ActivityIndicator, AppState, FlatList, Pressable, StyleSheet, Text, View,
  type NativeScrollEvent, type NativeSyntheticEvent,
} from "react-native";
import { KeyboardAvoidingView } from "react-native-keyboard-controller";

import { TimelineSkeleton } from "../../../components/chat/TimelineSkeleton";
import { ConversationHeader } from "../../../components/conversation/ConversationHeader";
import { Avatar } from "../../../components/room/Avatar";
import { SessionOverview } from "../../../components/session/SessionOverview";
import { SessionPromptRow } from "../../../components/session/SessionPromptRow";
import { PendingPromptRow, TranscriptRowView, WorkingRow } from "../../../components/session/TranscriptRow";
import { BottomSheet } from "../../../components/ui/BottomSheet";
import { chatPalette, colors, fonts, radii, spacing, useChatPalette } from "../../../config/theme";
import { markSessionsSeenOnDevice, memberName } from "../../../lib/localSessions";
import { usePushedScreenKeyboardOffset } from "../../../lib/keyboardOffset";
import { useMobileRooms } from "../../../lib/rooms";
import { fetchMembers, useTeamsConfig, type CompanyMember } from "../../../lib/teamsData";

/** Distance from the bottom, in px, still counted as "at the bottom" (the room screen's value). */
const BOTTOM_SLACK = 80;

/**
 * One shared Claude Code / Codex session, drawn in its TUI's grammar. Sessions
 * are workspace-scoped, not a room's: the session row always comes fresh from
 * the workspace session list (status, generation, control availability), and
 * only the sharer gets the prompt row. The body remounts when the session
 * restarts, exactly as the web pane does.
 */
export default function SessionScreen() {
  const { sessionId } = useLocalSearchParams<{ sessionId: string }>();
  const { userId } = useAuth();
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
  const fetchedFor = useRef<string | null>(null);
  const [fallback, setFallback] = useState<{ sessionId: string; session: LocalSession | null; notFound: boolean } | null>(null);
  useEffect(() => {
    if (listed || coldLoading || !sessionId || fetchedFor.current === sessionId) return;
    fetchedFor.current = sessionId;
    let alive = true;
    void rooms.resourceSource.client.getLocalSession(sessionId)
      .then((s) => { if (alive) setFallback({ sessionId, session: s, notFound: false }); })
      .catch((e) => { if (alive) setFallback({ sessionId, session: null, notFound: e instanceof ApiError && e.status === 404 }); });
    return () => { alive = false; };
  }, [listed, coldLoading, sessionId, rooms.resourceSource.client]);
  const resolvedFallback = fallback?.sessionId === sessionId ? fallback : null;
  const session = listed ?? resolvedFallback?.session ?? undefined;
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
  // Hoisted above the early returns below — see the room screen.
  const keyboardOffset = usePushedScreenKeyboardOffset();
  const [detailsOpen, setDetailsOpen] = useState(false);

  const headerOptions = {
    // The back button is the (app) Stack's own: this is a pushed screen.
    title: session ? sessionTitle(session) : "Session",
    // Avatar (the tool's mark, ringed by its live state) + name + where it runs;
    // tapping opens the details sheet (today's summary lines and SessionOverview).
    headerTitle: session ? () => (
      <ConversationHeader
        title={sessionLabel(session)}
        avatar={<Avatar name={sessionLabel(session)} kind="agent" vendor={session.provider} ring={sessionRingState(session)} size={28} palette={palette} />}
        status={`on ${own ? "your" : `${ownerName}’s`} Mac · ${sessionStatus[session.status].toLowerCase()}`}
        statusTone={session.status === "busy" ? "live" : "mute"}
        palette={palette}
        onPress={() => setDetailsOpen(true)}
      />
    ) : undefined,
    // Only when someone ELSE is sharing this session -- no "message yourself".
    headerRight: session && session.humanUserId !== userId ? () => (
      <Pressable
        accessibilityRole="button"
        accessibilityLabel={`Message ${ownerName}`}
        // 20pt icon + 12pt hitSlop on every side reaches the 44pt tap-target floor.
        hitSlop={12}
        onPress={messageOwner}
      >
        <SymbolView accessibilityElementsHidden name="bubble.left" size={20} tintColor={colors.inkSoft} />
      </Pressable>
    ) : undefined,
  };

  if (!session) {
    return (
      <View style={styles.container}>
        <Stack.Screen options={headerOptions} />
        <View style={styles.centered}>
          {workspace.error ? (
            <>
              <Text accessibilityRole="alert" style={styles.errorText}>{workspace.error}</Text>
              <Pressable onPress={() => void workspace.refresh()} accessibilityRole="button" accessibilityLabel="Try again">
                <Text style={styles.link}>Try again</Text>
              </Pressable>
            </>
          ) : resolvedFallback?.notFound ? (
            <Text style={styles.meta}>This session is no longer shared.</Text>
          ) : (
            <TimelineSkeleton />
          )}
        </View>
      </View>
    );
  }
  return (
    <View style={styles.container}>
      <Stack.Screen options={headerOptions} />
      {rooms.selectRoomError && (
        // A failed DM open (the header button) is transient and must not hide
        // the transcript underneath a full-screen blocked state -- see
        // sessions.tsx's identical inline treatment of selectRoomError.
        <View style={styles.banner}>
          <Text accessibilityRole="alert" style={styles.errorText}>{rooms.selectRoomError}</Text>
        </View>
      )}
      <SessionBody
        key={sessionViewKey(session)}
        session={session}
        client={rooms.resourceSource.client}
        revision={workspace.hintRevision}
        onAccessLost={workspace.refresh}
        ownerName={ownerName}
        own={own}
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
      />
    </View>
  );
}

function SessionBody({
  session, client, revision, onAccessLost, ownerName, own, name, isMine, onMessageOwner, keyboardOffset, detailsOpen, onOpenDetails, onCloseDetails, pollMs,
}: {
  session: LocalSession;
  client: SessionSource["client"];
  revision: number;
  onAccessLost: () => void;
  ownerName: string;
  own: boolean;
  name: (who: string | null) => string;
  isMine: (who: string | null) => boolean;
  onMessageOwner: () => void;
  keyboardOffset: number;
  detailsOpen: boolean;
  onOpenDetails: () => void;
  onCloseDetails: () => void;
  pollMs: number;
}) {
  const palette = useChatPalette();
  const busy = session.status === "busy";
  const history = useLocalSessionHistory(client, session.sessionId, session.latestEventSeq, revision, onAccessLost, { pollMs });
  const [text, setText] = useState("");
  const command = useSessionCommand(client, session, { uuid: randomUUID, onAccessLost, onSent: () => setText(""), agentName: name });
  // The session's own events, for its pending prompt and wait; a subagent's are tagged with it.
  const events = useMemo(() => mainEvents(history.events), [history.events]);
  const pendingText = usePendingPrompt(command.attempt, events);
  const pendingShown = !!pendingText && !history.hasNewer;
  const waitingOn = session.status === "waiting-approval" ? pendingApproval(events) ?? "" : null;
  const last = events.at(-1);
  const turnOpen = busy && !history.hasNewer && last?.lifecycle !== "turn_end" && last?.lifecycle !== "turn_aborted";
  // From every event: a subagent is drawn inline where it was launched and opens onto its own rows.
  const rows = useMemo(() => groupRows(buildRows(history.events), turnOpen), [history.events, turnOpen]);

  const listRef = useRef<FlatList<TranscriptItem>>(null);
  const atBottomRef = useRef(true);
  const [atBottom, setAtBottom] = useState(true);
  const onScroll = useCallback((event: NativeSyntheticEvent<NativeScrollEvent>) => {
    const { contentOffset, contentSize, layoutMeasurement } = event.nativeEvent;
    const next = contentSize.height - layoutMeasurement.height - contentOffset.y <= BOTTOM_SLACK;
    if (next !== atBottomRef.current) {
      atBottomRef.current = next;
      setAtBottom(next);
    }
  }, []);
  const loadOlder = useCallback(() => {
    if (history.hasOlder && !history.loading) void history.load("older");
  }, [history]);

  return (
    <KeyboardAvoidingView
      style={styles.container}
      behavior="padding"
      keyboardVerticalOffset={keyboardOffset}
    >
      {waitingOn !== null && (
        // Impossible to miss, but no remote approve: approvals stay in the
        // terminal. The full reason plus the "Message owner" link live in the
        // details sheet -- this is a one-line pointer to it.
        <View style={[styles.waitingBanner, { backgroundColor: palette.surface, borderColor: palette.alert }]} accessibilityLiveRegion="polite">
          <Text style={[styles.waitingBannerText, { color: palette.fg }]} numberOfLines={1}>
            {own ? "Waiting on you in your terminal" : `Waiting on ${ownerName} in their terminal`}
          </Text>
          <Pressable onPress={onOpenDetails} accessibilityRole="button" accessibilityLabel="Details" hitSlop={8}>
            <Text style={[styles.bannerLink, { color: palette.link }]}>Details</Text>
          </Pressable>
        </View>
      )}
      <FlatList
        ref={listRef}
        testID="session-transcript"
        data={rows}
        keyExtractor={(row) => String(row.seq)}
        contentContainerStyle={[styles.list, rows.length === 0 && styles.listEmpty]}
        keyboardDismissMode="interactive"
        keyboardShouldPersistTaps="handled"
        onScroll={onScroll}
        scrollEventThrottle={16}
        onStartReached={loadOlder}
        onStartReachedThreshold={0.2}
        maintainVisibleContentPosition={{ minIndexForVisible: 0 }}
        onContentSizeChange={() => {
          if (atBottomRef.current) listRef.current?.scrollToEnd({ animated: false });
        }}
        ListHeaderComponent={
          <View>
            {history.loading && history.hasOlder && <ActivityIndicator style={styles.loadingOlder} color={palette.fgMute} />}
            {history.error && (
              <View style={styles.errorCard}>
                <Text accessibilityRole="alert" style={styles.errorText}>{history.error}</Text>
                <Pressable onPress={() => void history.retry()} accessibilityRole="button" accessibilityLabel="Retry history">
                  <Text style={styles.link}>Retry</Text>
                </Pressable>
              </View>
            )}
          </View>
        }
        ListEmptyComponent={
          history.loading ? <TimelineSkeleton /> : history.error ? null : <Text style={styles.meta}>No shared output yet.</Text>
        }
        ListFooterComponent={
          <View>
            {turnOpen && <WorkingRow provider={session.provider} since={last?.ts} />}
            {pendingShown && <PendingPromptRow provider={session.provider} text={pendingText} ownerName={ownerName} chip={command.chip} />}
            {history.hasNewer && (
              <Pressable disabled={history.loading} onPress={() => void history.load("latest")} accessibilityRole="button" accessibilityLabel="Jump to latest">
                <Text style={styles.link}>More recent activity is available. Jump to latest</Text>
              </Pressable>
            )}
          </View>
        }
        renderItem={({ item, index }) => (
          <TranscriptRowView provider={session.provider} row={item} name={name} isMine={isMine} live={turnOpen && index === rows.length - 1} events={history.events} status={session.status} />
        )}
      />
      {!atBottom && !history.hasNewer && (
        <Pressable
          style={[styles.jump, { backgroundColor: palette.fg }]}
          onPress={() => listRef.current?.scrollToEnd({ animated: true })}
          accessibilityRole="button"
          accessibilityLabel="Scroll to bottom"
        >
          <SymbolView accessibilityElementsHidden name="arrow.down" size={16} tintColor={palette.onFg} />
        </Pressable>
      )}
      {session.status === "ended" ? null : own ? <SessionPromptRow session={session} text={text} onChangeText={setText} command={command} pendingShown={pendingShown} />
        : <Text style={[styles.meta, styles.readOnly]}>Only {ownerName} can send to this session.</Text>}
      <BottomSheet visible={detailsOpen} onClose={onCloseDetails} large>
        <View style={styles.sheetContent}>
          <View style={styles.sheetTextGroup}>
            <Text style={[styles.sheetHeadline, { color: chatPalette.light.fg }]}>
              {`${ownerName} · ${sessionStatus[session.status]}${session.cwdName ? ` · ~/${session.cwdName}` : ""}`}
            </Text>
            <Text style={[styles.sheetMeta, { color: chatPalette.light.fgSoft }]}>
              {`${session.providerVersion} · Follow-ups ${session.capabilities.sendMode === "queue" ? "queue" : "wait"} on ${own ? "your" : `${ownerName}’s`} Mac. Approvals stay in ${own ? "your" : "their"} terminal.${session.workspace?.overlap ? " Multiple sessions may overlap this checkout." : ""}`}
            </Text>
            {session.controlUnavailableReason && (
              <Text style={[styles.sheetMeta, { color: chatPalette.light.warn }]}>{unavailableReason[session.controlUnavailableReason]}</Text>
            )}
            {waitingOn !== null && (
              <View style={styles.waitingBlock} accessibilityLiveRegion="polite">
                <Text style={[styles.waitingTitle, { color: chatPalette.light.fg }]}>{own ? "Waiting on you in your terminal" : `Waiting on ${ownerName} in their terminal`}</Text>
                {waitingOn ? <Text style={[styles.waitingReason, { color: chatPalette.light.fgSoft }]}>{waitingOn}</Text> : null}
                {own ? <Text style={[styles.sheetMeta, { color: chatPalette.light.fgSoft }]}>Answer it on your Mac; approvals are not sent from here.</Text> : (
                  <Pressable onPress={onMessageOwner} accessibilityRole="button" accessibilityLabel={`Message ${ownerName}`}>
                    <Text style={[styles.bannerLink, { color: chatPalette.light.link }]}>Message {ownerName}</Text>
                  </Pressable>
                )}
              </View>
            )}
          </View>
          <SessionOverview events={events} />
        </View>
      </BottomSheet>
    </KeyboardAvoidingView>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: colors.cream },
  centered: { flex: 1, padding: spacing.lg, gap: spacing.sm },
  list: { paddingHorizontal: spacing.md, paddingTop: spacing.sm, paddingBottom: spacing.lg },
  listEmpty: { flexGrow: 1 },
  meta: { fontFamily: fonts.sans, fontSize: 12, lineHeight: 16, color: colors.mute },
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
  waitingBannerText: { flex: 1, fontFamily: fonts.sansSemiBold, fontSize: 14, lineHeight: 18 },
  waitingBlock: { gap: 2, marginTop: spacing.xs },
  waitingTitle: { fontFamily: fonts.sansSemiBold, fontSize: 14, lineHeight: 18 },
  waitingReason: { fontFamily: fonts.mono, fontSize: 12, lineHeight: 16 },
  readOnly: { paddingHorizontal: spacing.lg, paddingVertical: spacing.md },
  errorCard: { paddingVertical: spacing.sm, gap: spacing.xs },
  errorText: { fontFamily: fonts.sans, fontSize: 13, lineHeight: 18, color: colors.errorText },
  link: { fontFamily: fonts.sansSemiBold, fontSize: 13, lineHeight: 18, color: colors.forest, paddingVertical: spacing.xs },
  bannerLink: { fontFamily: fonts.sansSemiBold, fontSize: 13, lineHeight: 18 },
  jump: {
    position: "absolute",
    right: spacing.lg,
    bottom: 96,
    width: 36,
    height: 36,
    borderRadius: 18,
    alignItems: "center",
    justifyContent: "center",
  },
  loadingOlder: { paddingVertical: spacing.sm },
  sheetContent: { gap: spacing.md, paddingBottom: spacing.lg },
  sheetTextGroup: { paddingHorizontal: spacing.lg, gap: 4 },
  sheetHeadline: { fontFamily: fonts.sansMedium, fontSize: 15, lineHeight: 20 },
  sheetMeta: { fontFamily: fonts.sans, fontSize: 13, lineHeight: 18 },
});
