import { useAuth } from "@clerk/clerk-expo";
import { SymbolView } from "expo-symbols";
import { roomDisplayName, useWorkspaceSessions, type RoomSummary } from "@lightbulb/shared";
import { router, useFocusEffect } from "expo-router";
import { useCallback, useMemo, useRef, useState } from "react";
import {
  Animated,
  FlatList,
  Pressable,
  StyleSheet,
  Text,
  TextInput,
  View,
} from "react-native";

import { usePulse } from "../../../components/chat/usePulse";
import { LogoMark } from "../../../components/LogoMark";
import { NewMessageSheet } from "../../../components/room/NewMessageSheet";
import { RoomActionsSheet } from "../../../components/room/RoomActionsSheet";
import { RoomRow, byRecency, otherMembers } from "../../../components/room/RoomRow";
import { colors, fonts, radii, spacing, type } from "../../../config/theme";
import { AnimatedPressable, usePressScale } from "../../../lib/pressScale";
import { useMobileRooms } from "../../../lib/rooms";

/** One flattened list entry — a section header, or a conversation. */
type Row = { kind: "header"; title: string } | { kind: "room"; room: RoomSummary };

const SECTION_ORDER = ["Starred", "Channels", "Direct messages"] as const;

/**
 * Home's three sections, newest activity first inside each. A starred room
 * leaves both of the other two — it is in exactly one section, the way the web
 * sidebar's `groupRooms` does it.
 *
 * ponytail: owner-defined sections (`room.sectionId`, and the `sections` the
 * room list already returns) are Phase B; they slot in between Starred and
 * Channels when they ship.
 */
export function sectionRooms(rooms: readonly RoomSummary[]): {
  starred: RoomSummary[];
  channels: RoomSummary[];
  direct: RoomSummary[];
} {
  const sorted = [...rooms].sort(byRecency);
  return {
    starred: sorted.filter((room) => room.starred),
    channels: sorted.filter((room) => !room.starred && room.kind === "channel"),
    direct: sorted.filter((room) => !room.starred && room.kind !== "channel"),
  };
}

/** Pulsing placeholder rows while the conversation list is in flight. */
function ListSkeleton() {
  const pulse = usePulse(true, 0.55);
  return (
    <View style={styles.skeleton}>
      {[0, 1, 2, 3, 4].map((i) => (
        <Animated.View key={i} style={[styles.skeletonRow, { opacity: pulse }]} />
      ))}
    </View>
  );
}

/**
 * The conversation list. `direct` is the DMs tab: the same rows restricted to
 * `dm`/`group-dm`, unsectioned.
 *
 * ponytail: one component with one flag, because the two tabs differ only in
 * sectioning — split them if DMs ever grows a surface of its own.
 */
export function RoomsScreen({ direct = false }: { direct?: boolean }) {
  const { userId } = useAuth();
  const [focused, setFocused] = useState(true);
  useFocusEffect(
    useCallback(() => {
      setFocused(true);
      return () => setFocused(false);
    }, []),
  );
  const rooms = useMobileRooms({ autoSelect: false, subscribeToRoom: false });
  const { sessions } = useWorkspaceSessions(rooms.resourceSource, { enabled: focused });
  const sharing = useMemo(() => new Set(sessions.map((s) => s.humanUserId)), [sessions]);
  const [query, setQuery] = useState("");
  const [collapsed, setCollapsed] = useState<ReadonlySet<string>>(new Set());
  const [pickerOpen, setPickerOpen] = useState(false);
  // The room a long press is acting on; dropping it starts the sheet's close
  // animation. `lastActionRoom` keeps the sheet's content stable through that
  // close — RoomActionsSheet stays mounted and is only ever toggled via
  // `visible`, so it needs a non-null `room` for its whole exit animation.
  const [actionRoom, setActionRoom] = useState<RoomSummary | null>(null);
  const lastActionRoom = useRef<RoomSummary | null>(null);
  if (actionRoom) lastActionRoom.current = actionRoom;
  const pencilScale = usePressScale();

  const details = rooms.directoryRoomDetails;
  // A DM's name lives on the room DETAIL's member list, not on the summary.
  const participants = useCallback(
    (room: RoomSummary) =>
      otherMembers(details.get(room.roomId), userId).map((member) =>
        member.kind === "human" ? { ...member, sharing: sharing.has(member.id) } : member,
      ),
    [details, userId, sharing],
  );

  const data = useMemo<Row[]>(() => {
    // A channel's `#` is a glyph, not part of its name, so a typed one is
    // stripped rather than searched for.
    const needle = query.trim().replace(/^#/, "").toLowerCase();
    const visible = rooms.rooms.filter(
      (room) =>
        (!direct || room.kind !== "channel") &&
        (needle === "" ||
          roomDisplayName(room, participants(room)).toLowerCase().includes(needle)),
    );
    if (direct) {
      return [...visible].sort(byRecency).map((room) => ({ kind: "room", room }));
    }
    const sections = sectionRooms(visible);
    const byTitle = {
      Starred: sections.starred,
      Channels: sections.channels,
      "Direct messages": sections.direct,
    };
    return SECTION_ORDER.flatMap((title): Row[] => {
      const list = byTitle[title];
      if (list.length === 0) return [];
      return [
        { kind: "header", title },
        ...(collapsed.has(title)
          ? []
          : list.map((room): Row => ({ kind: "room", room }))),
      ];
    });
  }, [collapsed, direct, participants, query, rooms.rooms]);

  const toggle = useCallback((title: string) => {
    setCollapsed((current) => {
      const next = new Set(current);
      if (!next.delete(title)) next.add(title);
      return next;
    });
  }, []);

  return (
    <View style={styles.page}>
      <View style={styles.header}>
        <View style={styles.searchPill}>
          <SymbolView accessibilityElementsHidden name="magnifyingglass" size={14} tintColor={colors.placeholder} />
          <TextInput
            style={styles.searchInput}
            placeholder="Search"
            placeholderTextColor={colors.placeholder}
            value={query}
            onChangeText={setQuery}
            autoCorrect={false}
            returnKeyType="search"
          />
        </View>
        <AnimatedPressable
          onPress={() => setPickerOpen(true)}
          onPressIn={pencilScale.onPressIn}
          onPressOut={pencilScale.onPressOut}
          accessibilityRole="button"
          accessibilityLabel="New message"
          style={[styles.pencil, pencilScale.style]}
        >
          <SymbolView accessibilityElementsHidden name="square.and.pencil" size={16} tintColor={colors.cream} />
        </AnimatedPressable>
      </View>
      <FlatList
        contentInsetAdjustmentBehavior="automatic"
        automaticallyAdjustKeyboardInsets
        data={data}
        keyExtractor={(row) => (row.kind === "header" ? row.title : row.room.roomId)}
        contentContainerStyle={[styles.list, data.length === 0 && styles.listGrow]}
        keyboardShouldPersistTaps="handled"
        // A hard failure over a cached list reports itself ABOVE the rows
        // rather than replacing them — the rooms we already have are still the
        // truest thing on screen (same idiom the retired roster used).
        ListHeaderComponent={
          rooms.roomsError ? (
            <View style={styles.errorCard}>
              <Text style={styles.errorText}>
                Couldn’t load conversations: {rooms.roomsError.message}
              </Text>
              <Pressable
                onPress={() => void rooms.refreshRooms()}
                accessibilityRole="button"
                accessibilityLabel="Retry loading conversations"
              >
                <Text style={styles.retryText}>Retry</Text>
              </Pressable>
            </View>
          ) : null
        }
        ListEmptyComponent={
          rooms.roomsError ? null : rooms.roomsLoading ? (
            <ListSkeleton />
          ) : (
            <View style={styles.empty}>
              <LogoMark size={48} />
              <Text style={styles.dim}>
                {query.trim()
                  ? "No conversations match that."
                  : direct
                    ? "No direct messages yet."
                    : "No conversations yet."}
              </Text>
            </View>
          )
        }
        renderItem={({ item }) =>
          item.kind === "header" ? (
            <Pressable
              onPress={() => toggle(item.title)}
              accessibilityRole="button"
              accessibilityState={{ expanded: !collapsed.has(item.title) }}
              style={({ pressed }) => [styles.sectionRow, pressed && styles.sectionPressed]}
            >
              <SymbolView
                accessibilityElementsHidden
                name={collapsed.has(item.title) ? "chevron.right" : "chevron.down"}
                size={14}
                tintColor={colors.muteSoft}
              />
              <Text style={styles.sectionLabel}>{item.title}</Text>
            </Pressable>
          ) : (
            <RoomRow
              room={item.room}
              participants={participants(item.room)}
              unread={item.room.unreadCount}
              muted={item.room.muted}
              onPress={() => router.push(`/(app)/room/${item.room.roomId}`)}
              onLongPress={() => setActionRoom(item.room)}
            />
          )
        }
      />
      <NewMessageSheet
        visible={pickerOpen}
        onClose={() => setPickerOpen(false)}
        onOpenRoom={(roomId) => {
          setPickerOpen(false);
          router.push(`/(app)/room/${roomId}`);
        }}
      />
      {(() => {
        // Local const so TS narrows it through the closures below — see the
        // room screen's identical `sheetMessage` pattern.
        const sheetRoom = lastActionRoom.current;
        if (!sheetRoom) return null;
        return (
          <RoomActionsSheet
            room={sheetRoom}
            visible={actionRoom !== null}
            onClose={() => setActionRoom(null)}
            onSetPrefs={(prefs) => rooms.setRoomPrefs(sheetRoom.roomId, prefs)}
            onMarkUnread={() => rooms.markUnread(sheetRoom.roomId)}
          />
        );
      })()}
    </View>
  );
}

export default function Home() {
  return <RoomsScreen />;
}

const styles = StyleSheet.create({
  page: { flex: 1, backgroundColor: colors.cream },
  header: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.sm,
    paddingHorizontal: spacing.lg,
    paddingVertical: spacing.sm,
  },
  searchPill: {
    flex: 1,
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.sm,
    paddingHorizontal: spacing.md,
    height: 36,
    borderRadius: radii.md,
    backgroundColor: colors.creamDeeper,
  },
  searchInput: {
    flex: 1,
    fontFamily: fonts.sans,
    fontSize: 15,
    color: colors.ink,
    padding: 0,
  },
  pencil: {
    width: 36,
    height: 36,
    borderRadius: radii.md,
    backgroundColor: colors.forest,
    alignItems: "center",
    justifyContent: "center",
  },
  list: { paddingBottom: spacing.xl },
  listGrow: { flexGrow: 1 },
  sectionRow: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.sm,
    paddingHorizontal: spacing.lg,
    paddingTop: spacing.lg,
    paddingBottom: spacing.xs,
  },
  sectionPressed: { backgroundColor: colors.rowPressed },
  sectionLabel: {
    fontFamily: fonts.sansSemiBold,
    fontSize: 11,
    lineHeight: 15,
    letterSpacing: 0.22,
    color: colors.mute,
  },

  empty: { flex: 1, alignItems: "center", justifyContent: "center", gap: spacing.sm },
  dim: { ...type.body, color: colors.mute, textAlign: "center" },

  skeleton: { gap: spacing.md, padding: spacing.lg },
  skeletonRow: { height: 28, borderRadius: radii.sm, backgroundColor: colors.skeleton },

  errorCard: {
    margin: spacing.lg,
    padding: spacing.lg,
    backgroundColor: colors.errorBg,
    borderWidth: 1,
    borderColor: colors.errorBorder,
    borderRadius: radii.md,
  },
  errorText: { ...type.body, color: colors.errorText },
  retryText: {
    fontFamily: fonts.sansMedium,
    fontSize: 14,
    lineHeight: 20,
    color: colors.forest,
    marginTop: spacing.sm,
  },
});
