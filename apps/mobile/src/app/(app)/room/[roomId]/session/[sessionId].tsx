import Feather from "@expo/vector-icons/Feather";
import {
  buildRows, sessionLabel, sessionStatus, unavailableReason, useLocalSessionHistory, useRoomResources, useSessionCommand,
  type LocalSession, type RoomResourceSource, type TranscriptRow,
} from "@lightbulb/shared";
import { randomUUID } from "expo-crypto";
import { Stack, useFocusEffect, useLocalSearchParams } from "expo-router";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  AppState, FlatList, KeyboardAvoidingView, Platform, Pressable, StyleSheet, Text, View,
  type NativeScrollEvent, type NativeSyntheticEvent,
} from "react-native";

import { TimelineSkeleton } from "../../../../../components/chat/TimelineSkeleton";
import { HeaderBack } from "../../../../../components/room/HeaderBack";
import { RoomBlockedState } from "../../../../../components/room/RoomBlockedState";
import { useComposerKeyboardOffset } from "../../../../../components/room/RoomComposer";
import { SessionPromptRow } from "../../../../../components/session/SessionPromptRow";
import { SessionStatusDot } from "../../../../../components/session/SessionStatusDot";
import { TranscriptRowView, WorkingRow } from "../../../../../components/session/TranscriptRow";
import { colors, fonts, spacing } from "../../../../../config/theme";
import { memberName } from "../../../../../lib/localSessions";
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
  const [focused, setFocused] = useState(true);
  useFocusEffect(
    useCallback(() => {
      setFocused(true);
      return () => setFocused(false);
    }, []),
  );
  // A backgrounded app stops tailing; jest-expo reports an undefined state, which counts as active.
  const [appActive, setAppActive] = useState(AppState.currentState !== "background");
  useEffect(() => {
    const subscription = AppState.addEventListener("change", (state) => setAppActive(state === "active"));
    return () => subscription.remove();
  }, []);
  const live = focused && appActive;
  const rooms = useMobileRooms({ subscribeToRoom: focused, autoSelect: false, visible: false });
  const selectRoomRef = useRef(rooms.selectRoom);
  selectRoomRef.current = rooms.selectRoom;
  useEffect(() => {
    if (roomId) selectRoomRef.current(roomId);
  }, [roomId]);
  const resources = useRoomResources(roomId ?? "", rooms.resourceSource, { enabled: live && !!roomId });
  const session = resources.data?.sessions.find((s) => s.sessionId === sessionId);
  const members = rooms.activeRoom?.members;
  const ownerName = session ? memberName(members, session.humanUserId) : "Room member";
  const name = useCallback(
    (who: string | null) => (who ? members?.find((m) => m.id === who)?.displayName ?? "Teammate" : ownerName),
    [members, ownerName],
  );
  const keyboardOffset = useComposerKeyboardOffset();

  const headerOptions = {
    title: session ? sessionLabel(session) : "Session",
    headerLeft: () => <HeaderBack fallback={`/(app)/room/${roomId}/devices`} />,
  };

  if (rooms.accessRemoved || rooms.roomError || rooms.selectRoomError) {
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
      <SessionBody
        key={`${session.sessionId}:${session.generation}:${resources.accessRevision}`}
        roomId={roomId ?? ""}
        session={session}
        client={rooms.resourceSource.client}
        revision={resources.revision}
        onAccessLost={resources.revoke}
        live={live}
        ownerName={ownerName}
        name={name}
        keyboardOffset={keyboardOffset}
      />
    </View>
  );
}

function SessionBody({ roomId, session, client, revision, onAccessLost, live, ownerName, name, keyboardOffset }: {
  roomId: string;
  session: LocalSession;
  client: RoomResourceSource["client"];
  revision: number;
  onAccessLost: () => void;
  live: boolean;
  ownerName: string;
  name: (who: string | null) => string;
  keyboardOffset: number;
}) {
  const busy = session.status === "busy";
  const history = useLocalSessionHistory(client, roomId, session.sessionId, session.latestEventSeq, revision, onAccessLost, { busy: busy && live });
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
      behavior={Platform.OS === "ios" ? "padding" : undefined}
      keyboardVerticalOffset={keyboardOffset}
    >
      <View style={styles.summary}>
        <View style={styles.summaryLine}>
          {/* The text right beside it already states the status in words (and, while a
              turn is open, so does WorkingRow's own "Working" label below) — the dot's
              built-in accessibilityLabel would otherwise duplicate one of those. */}
          <View accessibilityElementsHidden importantForAccessibility="no-hide-descendants">
            <SessionStatusDot status={session.status} />
          </View>
          <Text style={styles.summaryText} numberOfLines={1}>
            {ownerName} · {sessionStatus[session.status]}{session.cwdName ? ` · ~/${session.cwdName}` : ""}
          </Text>
        </View>
        <Text style={styles.meta} numberOfLines={2}>
          {`${session.providerVersion} · Follow-ups ${session.capabilities.sendMode === "queue" ? "queue" : "wait"} on ${ownerName}’s Mac. Approvals stay in their terminal.${session.workspace?.overlap ? " Multiple sessions may overlap this checkout." : ""}`}
        </Text>
        {session.controlUnavailableReason && <Text style={styles.warn}>{unavailableReason[session.controlUnavailableReason]}</Text>}
      </View>
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
          <Feather name="arrow-down" size={16} color={colors.white} />
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
