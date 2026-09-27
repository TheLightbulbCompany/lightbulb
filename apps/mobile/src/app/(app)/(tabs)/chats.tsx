import { useAuth } from "@clerk/clerk-expo";
import { SymbolView } from "expo-symbols";
import {
  agentRingState,
  mergeChats,
  roomDisplayName,
  sessionLabel,
  sessionLive,
  sessionUnread,
  useWorkspaceSessions,
  type LocalSession,
  type RoomSummary,
  type SessionSeen,
} from "@lightbulb/shared";
import * as Clipboard from "expo-clipboard";
import { router, useFocusEffect, useIsFocused } from "expo-router";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Animated, FlatList, Pressable, StyleSheet, Text, View } from "react-native";
import type { SearchBarCommands } from "react-native-screens";

import { usePulse } from "../../../components/chat/usePulse";
import { RoomChatRow, SessionChatRow, type ChatRowAction } from "../../../components/chats/ChatRow";
import { PinnedStrip } from "../../../components/chats/PinnedStrip";
import { LogoMark } from "../../../components/LogoMark";
import { NewMessageSheet } from "../../../components/room/NewMessageSheet";
import { FocusedStatusBar } from "../../../components/ui/FocusedStatusBar";
import { fonts, radii, spacing, useChatPalette, type ChatPalette } from "../../../config/theme";
import {
  archiveSession,
  buildPinnedItems,
  movePin,
  otherMembers,
  pinKeyForRoom,
  readArchivedSessions,
  readPins,
  readShowRunningSessions,
  resolvePins,
  togglePin,
  writePins,
  type PinKey,
  type PinnedItem,
  type RowParticipant,
} from "../../../lib/chats";
import { roomIdsWithDrafts } from "../../../lib/composerDraft";
import { memberName, markSessionsSeenOnDevice } from "../../../lib/localSessions";
import { AnimatedPressable, usePressScale } from "../../../lib/pressScale";
import { useRoomActivity } from "../../../lib/roomActivity";
import { usePresence } from "../../../lib/presence";
import { useMobileRooms } from "../../../lib/rooms";
import { useTabHeader } from "../../../lib/tabHeader";
import { roomUrl } from "../../../lib/webLinks";
import { useWorkspaceRoster } from "../../../lib/workspaceRoster";
import { useLiveEvents } from "../../../providers/live-events";

/** RoomHeader.tsx's own copy — the same two strings the room screen's header shows. */
const STAR_FAILED = "The star could not be saved.";
const MUTE_FAILED = "The notification setting could not be saved.";

/** Pulsing placeholder rows while the conversation list is in flight. */
function ListSkeleton({ palette }: { palette: ChatPalette }) {
  const pulse = usePulse(true, 0.55);
  return (
    <View style={styles.skeleton}>
      {[0, 1, 2, 3, 4].map((i) => (
        <Animated.View
          key={i}
          style={[styles.skeletonRow, { opacity: pulse, backgroundColor: palette.raised }]}
        />
      ))}
    </View>
  );
}

/**
 * The header's compose button, top right, where iOS Messages keeps it.
 *
 * Reads its own palette rather than taking one as a prop: `useTabHeader` only
 * re-applies `headerRight` when the tab (re)selects, so a closure that
 * captured `palette` from the screen's own render would keep showing whatever
 * scheme was live at the LAST selection — stale until the user leaves and
 * comes back. Reading it here instead keeps that closure free of anything
 * that isn't a stable setter, and the button's own re-render (which the real
 * header still runs on every scheme change, same as any other component)
 * carries the current colours.
 */
function ComposeButton({ onPress }: { onPress: () => void }) {
  const palette = useChatPalette();
  const scale = usePressScale();
  return (
    <AnimatedPressable
      onPress={onPress}
      onPressIn={scale.onPressIn}
      onPressOut={scale.onPressOut}
      hitSlop={8}
      accessibilityRole="button"
      accessibilityLabel="New message"
      style={[styles.compose, scale.style]}
    >
      <SymbolView accessibilityElementsHidden name="square.and.pencil" size={18} tintColor={palette.fgSoft} />
    </AnimatedPressable>
  );
}

/**
 * The Chats tab: a pinned strip of starter agents / favourites / busy shared
 * sessions over ONE recency-sorted list merging every room and every live
 * workspace session (ended ones stay — owner ruling 2026-09-26). Works fully
 * with no `room.activity.changed` events ever arriving (a TestFlight build
 * against an older prod backend): rows just fall back to their last-message
 * preview and draw no ring.
 */
export default function Chats() {
  const palette = useChatPalette();
  const focused = useIsFocused();
  const { userId, orgId } = useAuth();
  const scope = orgId ?? userId ?? "none";

  const rooms = useMobileRooms({ autoSelect: false, subscribeToRoom: false });
  const { messaging } = useLiveEvents();
  const {
    sessions,
    refresh: refreshSessions,
    error: sessionsError,
  } = useWorkspaceSessions(rooms.resourceSource, { enabled: focused, includeEnded: true });
  const { agents, members } = useWorkspaceRoster(focused);
  const activity = useRoomActivity();
  // Always on (Finding 7, preflight.md): the tab stays mounted, and the
  // heartbeat itself already stops the moment the app is backgrounded.
  const online = usePresence(true);

  const draftScope = userId ? `${userId}:${orgId ?? userId}` : null;
  const [storedPins, setStoredPins] = useState<PinKey[] | null>(null);
  const [showRunning, setShowRunning] = useState(true);
  const [archived, setArchived] = useState<Record<string, number>>({});
  const [drafts, setDrafts] = useState<ReadonlySet<string>>(new Set());
  useFocusEffect(
    useCallback(() => {
      setStoredPins(readPins(scope));
      setShowRunning(readShowRunningSessions());
      setArchived(readArchivedSessions(scope));
      setDrafts(draftScope ? roomIdsWithDrafts(draftScope) : new Set());
    }, [scope, draftScope]),
  );

  const [seen, setSeen] = useState<SessionSeen>({});
  useEffect(() => {
    if (!focused) return;
    setSeen(markSessionsSeenOnDevice(sessions, null));
  }, [sessions, focused]);

  // The onboarded roster: a fresh workspace's starter agents (`origin: "Starter"`)
  // or a legacy/founder seed (`seeded`).
  const onboarded = useMemo(
    () => agents.filter((agent) => agent.seeded || agent.origin === "Starter").map((agent) => agent.id),
    [agents],
  );
  const pins = resolvePins(storedPins, onboarded);
  const editPins = useCallback(
    (next: PinKey[]) => {
      writePins(scope, next);
      setStoredPins(next);
    },
    [scope],
  );

  // The search field and the compose button live in the (app) Stack's header,
  // shared by all four tabs — see lib/tabHeader.ts.
  const [query, setQuery] = useState("");
  const [pickerOpen, setPickerOpen] = useState(false);
  const searchBar = useRef<SearchBarCommands>(null);
  useTabHeader(
    {
      headerLargeTitle: true,
      headerLargeTitleShadowVisible: false,
      headerLargeTitleStyle: { fontFamily: fonts.loraMedium },
      headerSearchBarOptions: {
        ref: searchBar,
        placeholder: "Search",
        autoCapitalize: "none",
        placement: "stacked",
        hideWhenScrolling: false,
        onChangeText: (event) => setQuery(event.nativeEvent.text),
        onCancelButtonPress: () => setQuery(""),
      },
      headerRight: () => <ComposeButton onPress={() => setPickerOpen(true)} />,
    },
    () => {
      searchBar.current?.cancelSearch();
      setQuery("");
    },
  );

  // This list is workspace-wide, so a live session can belong to someone the
  // viewer shares no room with — org roster first, falling back to room
  // members (same fallback the retired Shared Sessions screen used).
  const roomMembers = useMemo(() => rooms.rooms.flatMap((room) => room.members ?? []), [rooms.rooms]);
  const ownerName = useCallback(
    (humanUserId: string): string => {
      if (humanUserId === userId) return "You";
      const member = members.find((candidate) => candidate.userId === humanUserId);
      return member ? member.name || member.email || "Workspace member" : memberName(roomMembers, humanUserId);
    },
    [userId, members, roomMembers],
  );

  const needle = query.trim().replace(/^#/, "").toLowerCase();
  const entries = useMemo(() => {
    const merged = mergeChats(rooms.rooms, sessions, { archivedSessions: archived });
    if (!needle) return merged;
    return merged.filter((entry) =>
      entry.kind === "room"
        ? roomDisplayName(entry.room, otherMembers(entry.room, userId)).toLowerCase().includes(needle)
        : sessionLabel(entry.session).toLowerCase().includes(needle) ||
          ownerName(entry.session.humanUserId).toLowerCase().includes(needle),
    );
  }, [rooms.rooms, sessions, archived, needle, userId, ownerName]);

  const rosterById = useMemo(() => new Map(agents.map((agent) => [agent.id, agent])), [agents]);

  const rawPinnedItems = useMemo(
    () =>
      buildPinnedItems({
        pins,
        rooms: rooms.rooms,
        agents,
        sessions,
        showRunningSessions: showRunning,
        viewerId: userId,
      }),
    [pins, rooms.rooms, agents, sessions, showRunning, userId],
  );
  // The strip's ring is as live as the row's (Finding 21, preflight.md): a
  // roster-status ring only refreshes on the 30s poll, so a run that just
  // started shows "working" in the list before the strip catches up unless
  // this reads the same live signal.
  const pinnedItems = useMemo(
    () =>
      rawPinnedItems.map((item) =>
        item.kind === "agent"
          ? { ...item, ring: item.roomId && activity.working(item.roomId, item.agentId) ? ("running" as const) : item.ring }
          : item,
      ),
    [rawPinnedItems, activity],
  );

  const [reordering, setReordering] = useState(false);
  const onPinnedOpen = useCallback(
    (item: PinnedItem) => {
      if (item.kind === "session") {
        router.push(`/(app)/session/${item.session.sessionId}`);
        return;
      }
      if (item.kind === "room") {
        router.push(`/(app)/room/${item.room.roomId}`);
        return;
      }
      if (item.roomId) {
        router.push(`/(app)/room/${item.roomId}`);
        return;
      }
      void rooms
        .selectDm({ kind: "agent", id: item.agentId }, { activate: false })
        .then((id) => {
          if (id) router.push(`/(app)/room/${id}`);
        })
        .catch(() => {});
    },
    [rooms],
  );
  const onUnpin = useCallback((key: PinKey) => editPins(togglePin(pins, key)), [pins, editPins]);
  const onMove = useCallback(
    (key: PinKey, delta: -1 | 1) => {
      // Reorder over what the strip actually DRAWS (session pins excluded, stale
      // targets already dropped), or a move can swap with an invisible key and
      // the first/last disabled flags disagree with what's on screen (Finding
      // 11, preflight.md). Writing this list also prunes stale pins on edit.
      const drawnKeys = pinnedItems.filter((item) => item.kind !== "session").map((item) => item.key) as PinKey[];
      editPins(movePin(drawnKeys, key, delta));
    },
    [pinnedItems, editPins],
  );

  const [prefsError, setPrefsError] = useState<string | null>(null);
  const onRoomAction = useCallback(
    (room: RoomSummary, others: readonly RowParticipant[], action: ChatRowAction) => {
      switch (action) {
        case "markRead":
          void messaging
            .markRead(room.roomId, { lastReadSeq: room.lastMessageSeq })
            .then(() => rooms.refreshRooms())
            .catch(() => {});
          break;
        case "markUnread":
          void rooms.markUnread(room.roomId).catch(() => {});
          break;
        case "mute":
          setPrefsError(null);
          void rooms.setRoomPrefs(room.roomId, { muted: !room.muted }).catch(() => setPrefsError(MUTE_FAILED));
          break;
        case "star":
          setPrefsError(null);
          void rooms.setRoomPrefs(room.roomId, { starred: !room.starred }).catch(() => setPrefsError(STAR_FAILED));
          break;
        case "copyLink":
          void Clipboard.setStringAsync(roomUrl(room.roomId));
          break;
        case "pin":
          editPins(togglePin(pins, pinKeyForRoom(room, others)));
          break;
      }
    },
    [messaging, rooms, pins, editPins],
  );
  const onSessionAction = useCallback(
    (session: LocalSession, action: Extract<ChatRowAction, "markRead" | "archive">) => {
      if (action === "markRead") {
        setSeen(markSessionsSeenOnDevice([session], session.sessionId));
      } else {
        archiveSession(scope, session);
        setArchived(readArchivedSessions(scope));
      }
    },
    [scope],
  );

  const [refreshing, setRefreshing] = useState(false);
  const onRefresh = () => {
    setRefreshing(true);
    void Promise.all([rooms.refreshRooms(), refreshSessions()]).finally(() => setRefreshing(false));
  };

  return (
    <View style={[styles.page, { backgroundColor: palette.surface }]}>
      <FocusedStatusBar />
      <FlatList
        testID="chat-list"
        contentInsetAdjustmentBehavior="automatic"
        automaticallyAdjustKeyboardInsets
        data={entries}
        keyExtractor={(entry) => entry.key}
        contentContainerStyle={[styles.list, entries.length === 0 && styles.listGrow]}
        keyboardShouldPersistTaps="handled"
        refreshing={refreshing}
        onRefresh={onRefresh}
        // A stored pin the strip drops (stale target) or a load-time failure
        // is shown ABOVE the rows, same idiom as the failed-prefs banner.
        ListHeaderComponent={
          <>
            {query.trim() === "" ? (
              <PinnedStrip
                items={pinnedItems}
                palette={palette}
                reordering={reordering}
                onOpen={onPinnedOpen}
                onUnpin={onUnpin}
                onMove={onMove}
                onReorder={setReordering}
              />
            ) : null}
            {prefsError ? (
              <Text accessibilityRole="alert" style={[styles.prefsError, { color: palette.alert }]}>
                {prefsError}
              </Text>
            ) : null}
            {rooms.roomsError ? (
              <View style={[styles.errorCard, { backgroundColor: palette.raised, borderColor: palette.edge }]}>
                <Text style={[styles.errorText, { color: palette.alert }]}>
                  Couldn’t load conversations: {rooms.roomsError.message}
                </Text>
                <Pressable
                  onPress={() => void rooms.refreshRooms()}
                  accessibilityRole="button"
                  accessibilityLabel="Retry loading conversations"
                >
                  <Text style={[styles.retryText, { color: palette.link }]}>Retry</Text>
                </Pressable>
              </View>
            ) : null}
            {sessionsError ? (
              <View style={[styles.errorCard, { backgroundColor: palette.raised, borderColor: palette.edge }]}>
                <Text style={[styles.errorText, { color: palette.alert }]}>{sessionsError}</Text>
                <Pressable
                  onPress={() => void refreshSessions()}
                  accessibilityRole="button"
                  accessibilityLabel="Retry loading shared sessions"
                >
                  <Text style={[styles.retryText, { color: palette.link }]}>Retry</Text>
                </Pressable>
              </View>
            ) : null}
          </>
        }
        ListEmptyComponent={
          rooms.roomsError ? null : rooms.roomsLoading ? (
            <ListSkeleton palette={palette} />
          ) : (
            <View style={styles.empty}>
              <LogoMark size={48} />
              <Text style={[styles.dim, { color: palette.fgMute }]}>
                {query.trim() ? "No conversations match that." : "No conversations yet."}
              </Text>
            </View>
          )
        }
        renderItem={({ item }) => {
          if (item.kind === "session") {
            const session = item.session;
            return (
              <View testID={`chat-session-${session.sessionId}`}>
                <SessionChatRow
                  session={session}
                  ownerName={ownerName(session.humanUserId)}
                  own={session.humanUserId === userId}
                  unread={sessionUnread(session, seen) !== null}
                  palette={palette}
                  onAction={(action) => onSessionAction(session, action)}
                />
              </View>
            );
          }
          const room = item.room;
          const others = otherMembers(room, userId).map((member) =>
            member.kind === "human"
              ? {
                  ...member,
                  online: online.has(member.id),
                  sharing: sessions.some((s) => s.humanUserId === member.id && sessionLive(s)),
                }
              : member,
          );
          const dmAgent = room.kind === "dm" && others[0]?.kind === "agent" ? others[0] : null;
          const ring = dmAgent
            ? activity.working(room.roomId, dmAgent.id)
              ? "running"
              : agentRingState(rosterById.get(dmAgent.id)?.status)
            : null;
          const live = activity.liveLine(room.roomId, room.kind);
          const pinned = pins.includes(pinKeyForRoom(room, others));
          return (
            <View testID={`chat-room-${room.roomId}`}>
              <RoomChatRow
                room={room}
                others={others}
                viewerId={userId}
                ring={ring}
                live={live}
                hasDraft={drafts.has(room.roomId)}
                pinned={pinned}
                palette={palette}
                onAction={(action) => onRoomAction(room, others, action)}
              />
            </View>
          );
        }}
      />
      <NewMessageSheet
        visible={pickerOpen}
        onClose={() => setPickerOpen(false)}
        onOpenRoom={(roomId) => {
          setPickerOpen(false);
          router.push(`/(app)/room/${roomId}`);
        }}
      />
    </View>
  );
}

const styles = StyleSheet.create({
  page: { flex: 1 },
  compose: {
    width: 36,
    height: 36,
    borderRadius: radii.sm,
    alignItems: "center",
    justifyContent: "center",
  },
  list: { paddingBottom: spacing.xl },
  listGrow: { flexGrow: 1 },

  empty: { flex: 1, alignItems: "center", justifyContent: "center", gap: spacing.sm },
  dim: { fontFamily: fonts.sans, fontSize: 15, lineHeight: 22, textAlign: "center" },

  skeleton: { gap: spacing.md, padding: spacing.lg },
  skeletonRow: { height: 28, borderRadius: radii.sm },

  prefsError: {
    fontFamily: fonts.sans,
    fontSize: 13,
    lineHeight: 18,
    paddingHorizontal: spacing.lg,
    paddingTop: spacing.sm,
  },
  errorCard: {
    margin: spacing.lg,
    padding: spacing.lg,
    borderWidth: 1,
    borderRadius: radii.md,
  },
  errorText: { fontFamily: fonts.sans, fontSize: 15, lineHeight: 22 },
  retryText: {
    fontFamily: fonts.sansMedium,
    fontSize: 14,
    lineHeight: 20,
    marginTop: spacing.sm,
  },
});
