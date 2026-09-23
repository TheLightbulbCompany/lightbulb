import { useAuth } from "@clerk/clerk-expo";
import { SymbolView } from "expo-symbols";
import {
  buildRows, sessionLabel, sessionStatus, sessionViewKey, unavailableReason, useLocalSessionHistory, useRoomResources, useSessionCommand,
  type LocalSession, type RoomResourceSource, type TranscriptRow,
} from "@lightbulb/shared";
import { randomUUID } from "expo-crypto";
import { router, Stack, useIsFocused, useLocalSearchParams } from "expo-router";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  AppState, FlatList, Pressable, StyleSheet, Text, View,
  type NativeScrollEvent, type NativeSyntheticEvent,
} from "react-native";
import { KeyboardAvoidingView } from "react-native-keyboard-controller";

import { TimelineSkeleton } from "../../../../../components/chat/TimelineSkeleton";
import { RoomBlockedState } from "../../../../../components/room/RoomBlockedState";
import { SessionPromptRow } from "../../../../../components/session/SessionPromptRow";
import { SessionStatusDot } from "../../../../../components/session/SessionStatusDot";
import { TranscriptRowView, WorkingRow } from "../../../../../components/session/TranscriptRow";
import { colors, fonts, spacing } from "../../../../../config/theme";
import { memberName } from "../../../../../lib/localSessions";
import { usePushedScreenKeyboardOffset } from "../../../../../lib/keyboardOffset";
import { useMobileRooms } from "../../../../../lib/rooms";

/** Distance from the bottom, in px, still counted as "at the bottom" (the room screen's value). */
const BOTTOM_SLACK = 80;

/**
 * One shared Claude Code / Codex session, drawn in its TUI's grammar, with a
 * prompt row to send follow-ups. The session row itself always comes fresh
 * from the room's resources (status, generation, control availability); the
 * body remounts when the session restarts or the reader's access epoch changes,
 * exactly as the web pane does.
 */
export default function SessionScreen() {
  const { roomId, sessionId } = useLocalSearchParams<{ roomId: string; sessionId: string }>();
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
  const rooms = useMobileRooms({ subscribeToRoom: focused, autoSelect: false, visible: false });
  const selectRoomRef = useRef(rooms.selectRoom);
  selectRoomRef.current = rooms.selectRoom;
  useEffect(() => {
    if (roomId) selectRoomRef.current(roomId);
  }, [roomId]);
  const resources = useRoomResources(roomId ?? "", rooms.resourceSource, { enabled: live && !!roomId, includeEnded: true });
  const session = resources.data?.sessions.find((s) => s.sessionId === sessionId);
  const members = rooms.activeRoom?.members;
  const ownerName = session ? memberName(members, session.humanUserId) : "Room member";
  const own = session ? session.humanUserId === userId : false;
  const name = useCallback(
    (who: string | null) => (who ? members?.find((m) => m.id === who)?.displayName ?? "Teammate" : ownerName),
    [members, ownerName],
  );
  // Hoisted above the early returns below — see the room screen.
  const keyboardOffset = usePushedScreenKeyboardOffset();

  const headerOptions = {
    // The back button is the (app) Stack's own: this is a pushed screen.
    title: session ? sessionLabel(session) : "Session",
    // Only when someone ELSE is sharing this session -- no "message yourself".
    headerRight: session && session.humanUserId !== userId ? () => (
      <Pressable
        accessibilityRole="button"
        accessibilityLabel={`Message ${ownerName}`}
        // 20pt icon + 12pt hitSlop on every side reaches the 44pt tap-target floor.
        hitSlop={12}
        onPress={() =>
          void rooms
            .selectDm({ kind: "human", id: session.humanUserId }, { activate: false })
            .then((dm) => { if (dm) router.push(`/(app)/room/${dm}`); })
            // Rejection is already surfaced via rooms.selectRoomError below;
            // this only stops it from becoming an unhandled rejection.
            .catch(() => {})
        }
      >
        <SymbolView accessibilityElementsHidden name="bubble.left" size={20} tintColor={colors.inkSoft} />
      </Pressable>
    ) : undefined,
  };

  if (rooms.accessRemoved || rooms.roomError) {
    return (
      <View style={styles.container}>
        <Stack.Screen options={headerOptions} />
        <RoomBlockedState
          state={rooms.accessRemoved ? "access-removed" : "unavailable"}
          onRetry={rooms.accessRemoved ? undefined : rooms.refreshRoom}
        />
      </View>
    );
  }
  if (!session) {
    return (
      <View style={styles.container}>
        <Stack.Screen options={headerOptions} />
        <View style={styles.centered}>
          {resources.error ? (
            <>
              <Text accessibilityRole="alert" style={styles.errorText}>{resources.error}</Text>
              <Pressable onPress={() => void resources.refresh()} accessibilityRole="button" accessibilityLabel="Try again">
                <Text style={styles.link}>Try again</Text>
              </Pressable>
            </>
          ) : resources.loading || !resources.data ? (
            <TimelineSkeleton />
          ) : (
            <Text style={styles.meta}>This session is no longer shared.</Text>
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
        key={sessionViewKey(session, resources.accessRevision)}
        roomId={roomId ?? ""}
        session={session}
        client={rooms.resourceSource.client}
        revision={resources.revision}
        onAccessLost={resources.revoke}
        ownerName={ownerName}
        own={own}
        name={name}
        keyboardOffset={keyboardOffset}
        // Backstop poll while this screen is live AND the session is producing
        // output (same 5s cadence as web): transcript hints are throttled
        // server-side with no trailing edge. Idle sessions stay on hints plus
        // the resources safety poll. Stopped when the screen blurs or the app
        // backgrounds.
        pollMs={live && session.status === "busy" ? 5000 : 0}
      />
    </View>
  );
}

function SessionBody({ roomId, session, client, revision, onAccessLost, ownerName, own, name, keyboardOffset, pollMs }: {
  roomId: string;
  session: LocalSession;
  client: RoomResourceSource["client"];
  revision: number;
  onAccessLost: () => void;
  ownerName: string;
  own: boolean;
  name: (who: string | null) => string;
  keyboardOffset: number;
  pollMs: number;
}) {
  const busy = session.status === "busy";
  const history = useLocalSessionHistory(client, roomId, session.sessionId, session.latestEventSeq, revision, onAccessLost, { pollMs });
  const [text, setText] = useState("");
  const command = useSessionCommand(client, roomId, session, { uuid: randomUUID, onAccessLost, onSent: () => setText("") });
  const rows = useMemo(() => buildRows(history.events), [history.events]);
  const last = history.events.at(-1);
  const turnOpen = busy && !history.hasNewer && last?.lifecycle !== "turn_end" && last?.lifecycle !== "turn_aborted";

  const listRef = useRef<FlatList<TranscriptRow>>(null);
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

  return (
    <KeyboardAvoidingView
      style={styles.container}
      behavior="padding"
      keyboardVerticalOffset={keyboardOffset}
    >
      <View style={styles.summary}>
        <View style={styles.summaryLine}>
          <SessionStatusDot status={session.status} />
          <Text style={styles.summaryText} numberOfLines={1}>
            {ownerName} · {sessionStatus[session.status]}{session.cwdName ? ` · ~/${session.cwdName}` : ""}
          </Text>
        </View>
        <Text style={styles.meta} numberOfLines={2}>
          {`${session.providerVersion} · Follow-ups ${session.capabilities.sendMode === "queue" ? "queue" : "wait"} on ${own ? "your" : `${ownerName}’s`} Mac. Approvals stay in ${own ? "your" : "their"} terminal.${session.workspace?.overlap ? " Multiple sessions may overlap this checkout." : ""}`}
        </Text>
        {session.controlUnavailableReason && <Text style={styles.warn}>{unavailableReason[session.controlUnavailableReason]}</Text>}
      </View>
      <FlatList
        ref={listRef}
        data={rows}
        keyExtractor={(row) => String(row.seq)}
        contentContainerStyle={[styles.list, rows.length === 0 && styles.listEmpty]}
        keyboardDismissMode="interactive"
        keyboardShouldPersistTaps="handled"
        onScroll={onScroll}
        scrollEventThrottle={16}
        onContentSizeChange={() => {
          if (atBottomRef.current) listRef.current?.scrollToEnd({ animated: false });
        }}
        ListHeaderComponent={
          <View>
            {history.hasOlder && (
              <Pressable disabled={history.loading} onPress={() => void history.load("older")} accessibilityRole="button" accessibilityLabel="Load older">
                <Text style={styles.link}>Load older</Text>
              </Pressable>
            )}
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
            {history.hasNewer && (
              <Pressable disabled={history.loading} onPress={() => void history.load("latest")} accessibilityRole="button" accessibilityLabel="Jump to latest">
                <Text style={styles.link}>More recent activity is available. Jump to latest</Text>
              </Pressable>
            )}
          </View>
        }
        renderItem={({ item }) => <TranscriptRowView provider={session.provider} row={item} name={name} />}
      />
      {!atBottom && !history.hasNewer && (
        <Pressable
          style={styles.jump}
          onPress={() => listRef.current?.scrollToEnd({ animated: true })}
          accessibilityRole="button"
          accessibilityLabel="Scroll to bottom"
        >
          <SymbolView accessibilityElementsHidden name="arrow.down" size={16} tintColor={colors.white} />
        </Pressable>
      )}
      <SessionPromptRow session={session} text={text} onChangeText={setText} command={command} />
    </KeyboardAvoidingView>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: colors.cream },
  centered: { flex: 1, padding: spacing.lg, gap: spacing.sm },
  summary: {
    paddingHorizontal: spacing.lg,
    paddingVertical: spacing.sm,
    gap: 2,
    borderBottomWidth: StyleSheet.hairlineWidth,
    borderBottomColor: colors.border,
  },
  summaryLine: { flexDirection: "row", alignItems: "center", gap: spacing.sm },
  summaryText: { flex: 1, fontFamily: fonts.sansMedium, fontSize: 14, lineHeight: 18, color: colors.ink },
  list: { paddingHorizontal: spacing.md, paddingTop: spacing.sm, paddingBottom: spacing.lg },
  listEmpty: { flexGrow: 1 },
  meta: { fontFamily: fonts.sans, fontSize: 12, lineHeight: 16, color: colors.mute },
  warn: { fontFamily: fonts.sans, fontSize: 12, lineHeight: 16, color: colors.warn },
  // Full horizontal padding -- unlike errorCard below, this sits directly on
  // the screen, not inside the transcript's already-padded list.
  banner: { padding: spacing.lg, gap: spacing.xs },
  errorCard: { paddingVertical: spacing.sm, gap: spacing.xs },
  errorText: { fontFamily: fonts.sans, fontSize: 13, lineHeight: 18, color: colors.errorText },
  link: { fontFamily: fonts.sansSemiBold, fontSize: 13, lineHeight: 18, color: colors.forest, paddingVertical: spacing.xs },
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
