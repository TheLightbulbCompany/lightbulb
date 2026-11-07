import { useAuth } from "@clerk/expo";
import { SymbolView } from "../../../components/ui/SymbolView";
import {
  agentRingState,
  mergeChats,
  resolveCeoAgentId,
  roomDisplayName,
  sessionLive,
  sessionMatches,
  sessionUnread,
  useWorkspaceSessions,
  type LocalSession,
  type RoomSummary,
  type SessionSeen,
} from "@lightbulb/shared";
import * as Clipboard from "expo-clipboard";
import { router, useFocusEffect, useIsFocused, useLocalSearchParams } from "expo-router";
import { useCallback, useEffect, useMemo, useState } from "react";
import { Animated, AppState, FlatList, Pressable, StyleSheet, Text, View } from "react-native";

import { usePulse } from "../../../components/chat/usePulse";
import { TrialRecapCard } from "../../../components/billing/TrialRecapCard";
import { RoomChatRow, SessionChatRow, type ChatRowAction } from "../../../components/chats/ChatRow";
import { SessionRenameSheet } from "../../../components/session/SessionRenameSheet";
import { LogoMark } from "../../../components/LogoMark";
import { NewMessageSheet } from "../../../components/room/NewMessageSheet";
import { FocusedStatusBar } from "../../../components/ui/FocusedStatusBar";
import { fonts, radii, spacing, useChatPalette, type ChatPalette } from "../../../config/theme";
import { archiveSession, fileSpecialists, otherMembers, readArchivedSessions } from "../../../lib/chats";
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
function HeaderActions({ onMessage }: { onMessage: () => void }) {
  const palette = useChatPalette();
  const messageScale = usePressScale();
  return (
    <View style={styles.headerActions}>
      <AnimatedPressable onPress={onMessage} onPressIn={messageScale.onPressIn} onPressOut={messageScale.onPressOut}
        hitSlop={8} accessibilityRole="button" accessibilityLabel="New message" style={[styles.compose, messageScale.style]}>
        <SymbolView accessibilityElementsHidden name="square.and.pencil" size={18} tintColor={palette.fgSoft} />
      </AnimatedPressable>
    </View>
  );
}

type ChatFilter = "all" | "sessions" | "channels" | "direct";
const filters: readonly { id: ChatFilter; label: string }[] = [
  { id: "all", label: "All" }, { id: "sessions", label: "Sessions" },
  { id: "channels", label: "Channels" }, { id: "direct", label: "Direct" },
];

/**
 * The Chats tab: ONE recency-sorted list merging every room and every live
 * workspace session (ended ones stay — owner ruling 2026-09-26). No pinned
 * strip above it (owner ruling 2026-09-28): a running session is already a
 * row in the list. Works fully
 * with no `room.activity.changed` events ever arriving (a TestFlight build
 * against an older prod backend): rows just fall back to their last-message
 * preview and draw no ring.
 */
export default function Chats() {
  const palette = useChatPalette();
  const params = useLocalSearchParams<{ filter?: string }>();
  const focused = useIsFocused();
  const { userId, orgId } = useAuth();
  const [filter, setFilter] = useState<ChatFilter>("all");
  useEffect(() => {
    if (params.filter === "sessions") {
      setFilter("sessions");
      router.setParams({ filter: undefined });
    }
  }, [params.filter]);
  const scope = orgId ?? userId ?? "none";

  const rooms = useMobileRooms({ autoSelect: false, subscribeToRoom: false });
  const { messaging } = useLiveEvents();
  const {
    sessions,
    refresh: refreshSessions,
    error: sessionsError,
  } = useWorkspaceSessions(rooms.resourceSource, { enabled: focused, includeEnded: true });
  // `focused` stays true across a background trip, so the hook never re-enables:
  // refetch sessions on foreground, as the retired Shared Sessions screen did.
  useEffect(() => {
    if (!focused) return;
    const sub = AppState.addEventListener("change", (next) => {
      if (next === "active") void refreshSessions();
    });
    return () => sub.remove();
  }, [focused, refreshSessions]);
  const { agents, members } = useWorkspaceRoster(focused);
  const activity = useRoomActivity();
  // Always on (Finding 7, preflight.md): the tab stays mounted, and the
  // heartbeat itself already stops the moment the app is backgrounded.
  const online = usePresence(true);

  const draftScope = userId ? `${userId}:${orgId ?? userId}` : null;
  const [archived, setArchived] = useState<Record<string, number>>({});
  const [drafts, setDrafts] = useState<ReadonlySet<string>>(new Set());
  useFocusEffect(
    useCallback(() => {
      setArchived(readArchivedSessions(scope));
      setDrafts(draftScope ? roomIdsWithDrafts(draftScope) : new Set());
    }, [scope, draftScope]),
  );

  const [seen, setSeen] = useState<SessionSeen>({});
  useEffect(() => {
    if (!focused) return;
    setSeen(markSessionsSeenOnDevice(sessions, null));
  }, [sessions, focused]);

  // The search field and the compose button live in the (app) Stack's header,
  // shared by all four tabs — see lib/tabHeader.ts.
  const [query, setQuery] = useState("");
  const [pickerOpen, setPickerOpen] = useState(false);
  useTabHeader(
    {
      headerSearchBarOptions: {
        placeholder: "Search",
        autoCapitalize: "none",
        placement: "stacked",
        hideWhenScrolling: false,
        onChangeText: (event) => setQuery(event.nativeEvent.text),
        onCancelButtonPress: () => setQuery(""),
      },
      headerRight: () => <HeaderActions onMessage={() => setPickerOpen(true)} />,
    },
    () => {
      // No `cancelSearch()` here. This runs as the search bar is taken out of
      // the header (leaving the tab, switching workspace, a reload), and a
      // native command sent to a view in the middle of being removed crashed
      // the app: SIGSEGV in `uiManagerDidDispatchCommand`, twice on
      // 2026-09-28. The bar is rebuilt empty on return, so clearing the query
      // is all there is to do.
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
    return merged.filter((entry) => {
      if (filter === "sessions" && entry.kind !== "session") return false;
      if (filter === "channels" && (entry.kind !== "room" || entry.room.kind !== "channel")) return false;
      if (filter === "direct" && (entry.kind !== "room" || entry.room.kind === "channel")) return false;
      if (!needle) return true;
      return entry.kind === "room"
        ? roomDisplayName(entry.room, otherMembers(entry.room, userId)).toLowerCase().includes(needle)
        : sessionMatches(entry.session, needle, ownerName(entry.session.humanUserId));
    });
  }, [rooms.rooms, sessions, archived, filter, needle, userId, ownerName]);

  const rosterById = useMemo(() => new Map(agents.map((agent) => [agent.id, agent])), [agents]);

  // One voice. A DM with any agent but the front one (`@lightbulb`) files under a collapsed
  // Specialists row at the end. Until the front agent is known, every DM is listed.
  const frontAgentId = resolveCeoAgentId(agents);
  const [specialistsOpen, setSpecialistsOpen] = useState(false);
  const rows = useMemo(() => fileSpecialists(entries, frontAgentId, userId, specialistsOpen || !!needle),
    [entries, frontAgentId, specialistsOpen, needle, userId]);

  const [prefsError, setPrefsError] = useState<string | null>(null);
  const [renaming, setRenaming] = useState<LocalSession | null>(null);
  const onRoomAction = useCallback(
    (room: RoomSummary, action: ChatRowAction) => {
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
      }
    },
    [messaging, rooms],
  );
  const onSessionAction = useCallback(
    (session: LocalSession, action: Extract<ChatRowAction, "markRead" | "archive" | "rename">) => {
      if (action === "markRead") {
        setSeen(markSessionsSeenOnDevice([session], session.sessionId));
      } else if (action === "rename") {
        setRenaming(session);
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
        // Not `automaticallyAdjustKeyboardInsets`: when the keyboard closed over a
        // short list of search results it left them scrolled up under the header,
        // so pressing Search emptied the screen. Dragging the list puts the
        // keyboard away instead.
        keyboardDismissMode="on-drag"
        data={rows}
        keyExtractor={(entry) => entry.key}
        contentContainerStyle={[styles.list, entries.length === 0 && styles.listGrow]}
        keyboardShouldPersistTaps="handled"
        refreshing={refreshing}
        onRefresh={onRefresh}
        // A failure is shown ABOVE the rows.
        ListHeaderComponent={
          <>
            <TrialRecapCard />
            <View style={styles.filters} accessibilityRole="radiogroup" accessibilityLabel="Filter chats">
              {filters.map(({ id, label }) => (
                <Pressable key={id} onPress={() => setFilter(id)} accessibilityRole="radio"
                  accessibilityLabel={label} accessibilityState={{ selected: filter === id }}
                  style={[styles.filter, { backgroundColor: filter === id ? palette.selected : palette.surface }]}>
                  <Text style={{ color: filter === id ? palette.fg : palette.fgSoft,
                    fontFamily: filter === id ? fonts.sansMedium : fonts.sans, fontSize: 14 }}>{label}</Text>
                </Pressable>
              ))}
            </View>
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
                  accessibilityLabel="Retry loading chats"
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
                {query.trim() ? "No chats match that." : filter === "sessions" ? "No sessions yet." : filter === "channels" ? "No channels yet." : filter === "direct" ? "No direct messages yet." : "No chats yet."}
              </Text>
            </View>
          )
        }
        renderItem={({ item }) => {
          if (item.kind === "specialists") {
            const label = `Specialists · ${item.count}`;
            return (
              <Pressable testID="chat-specialists" accessibilityRole="button" accessibilityLabel={label}
                accessibilityState={{ expanded: item.open }} onPress={() => setSpecialistsOpen(!item.open)}
                style={styles.groupRow} hitSlop={4}>
                <Text style={[styles.groupLabel, { color: palette.fgMute }]}>{label}</Text>
                <Text style={[styles.groupLabel, { color: palette.fgMute }]}>{item.open ? "▾" : "▸"}</Text>
              </Pressable>
            );
          }
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
              // Super-agent: the front agent reads "Lightbulb" whatever its stored seat name ("CEO").
              : member.id === frontAgentId ? { ...member, displayName: "Lightbulb" } : member,
          );
          const dmAgent = room.kind === "dm" && others[0]?.kind === "agent" ? others[0] : null;
          const ring = dmAgent
            ? activity.working(room.roomId, dmAgent.id)
              ? "running"
              : agentRingState(rosterById.get(dmAgent.id)?.status)
            : null;
          const live = activity.liveLine(room.roomId, room.kind);
          return (
            <View testID={`chat-room-${room.roomId}`}>
              <RoomChatRow
                room={room}
                others={others}
                viewerId={userId}
                ring={ring}
                live={live}
                hasDraft={drafts.has(room.roomId)}
                sessions={sessions}
                palette={palette}
                onAction={(action) => onRoomAction(room, action)}
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
      <SessionRenameSheet session={renaming} visible={!!renaming} onClose={() => setRenaming(null)}
        onSave={async (title) => {
          if (!renaming) return;
          await rooms.resourceSource.client.setLocalSessionDisplayTitle(renaming.sessionId, title);
          await refreshSessions();
        }} />
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
  headerActions: { flexDirection: "row", gap: spacing.sm },
  filters: { flexDirection: "row", paddingHorizontal: spacing.md, paddingVertical: spacing.sm, gap: spacing.xs },
  filter: { minHeight: 36, flex: 1, alignItems: "center", justifyContent: "center", borderRadius: radii.sm },
  list: { paddingBottom: spacing.xl },
  listGrow: { flexGrow: 1 },
  groupRow: { flexDirection: "row", alignItems: "center", justifyContent: "space-between", minHeight: 44, paddingHorizontal: spacing.lg, marginTop: spacing.md },
  groupLabel: { fontFamily: fonts.sansSemiBold, fontSize: 10, letterSpacing: 1.2, lineHeight: 14, textTransform: "uppercase" },

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
