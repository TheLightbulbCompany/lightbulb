import { useAuth } from "@clerk/clerk-expo";
import { SymbolView } from "expo-symbols";
import {
  roomDisplayName,
  useWorkspaceSessions,
  type MemberPrefsRequest,
  type RoomSummary,
} from "@lightbulb/shared";
import * as Clipboard from "expo-clipboard";
import {
  router,
  useFocusEffect,
  useIsFocused,
  useNavigation,
  useRoute,
  type NativeStackNavigationProp,
} from "expo-router";
import { useNavigationState, type ParamListBase } from "expo-router/react-navigation";
import { useCallback, useLayoutEffect, useMemo, useRef, useState } from "react";
import { Animated, FlatList, Pressable, StyleSheet, Text, View } from "react-native";
import type { SearchBarCommands } from "react-native-screens";

import { usePulse } from "../../../components/chat/usePulse";
import { LogoMark } from "../../../components/LogoMark";
import { NewMessageSheet } from "../../../components/room/NewMessageSheet";
import {
  RoomListRow,
  byRecency,
  otherMembers,
  type RoomRowAction,
} from "../../../components/room/RoomRow";
import { colors, fonts, radii, spacing, type } from "../../../config/theme";
import { AnimatedPressable, usePressScale } from "../../../lib/pressScale";
import { roomIdsWithDrafts } from "../../../lib/composerDraft";
import { useMobileRooms } from "../../../lib/rooms";
import { roomUrl } from "../../../lib/webLinks";
import { useLiveEvents } from "../../../providers/live-events";

/** One flattened list entry — a section header, or a conversation. */
type Row = { kind: "header"; title: string } | { kind: "room"; room: RoomSummary };

const SECTION_ORDER = ["Starred", "Channels", "Direct messages"] as const;

/** RoomHeader.tsx's own copy — the same two strings the room screen's header shows. */
const STAR_FAILED = "The star could not be saved.";
const MUTE_FAILED = "The notification setting could not be saved.";

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

/** The header's compose button, top right, where iOS Messages keeps it. */
function ComposeButton({ onPress }: { onPress: () => void }) {
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
      <SymbolView accessibilityElementsHidden name="square.and.pencil" size={18} tintColor={colors.inkSoft} />
    </AnimatedPressable>
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
  const { userId, orgId } = useAuth();
  const focused = useIsFocused();
  const rooms = useMobileRooms({ autoSelect: false, subscribeToRoom: false });
  const { messaging } = useLiveEvents();
  const { sessions } = useWorkspaceSessions(rooms.resourceSource, { enabled: focused });
  const sharing = useMemo(() => new Set(sessions.map((s) => s.humanUserId)), [sessions]);
  // Re-read on focus, which is exactly when it can have changed: the only way
  // to leave a draft behind is to come back from the room you left it in.
  const [roomsWithDrafts, setRoomsWithDrafts] = useState<ReadonlySet<string>>(new Set<string>());
  const draftScope = userId ? `${userId}:${orgId ?? userId}` : null;
  useFocusEffect(
    useCallback(() => {
      setRoomsWithDrafts(draftScope ? roomIdsWithDrafts(draftScope) : new Set<string>());
    }, [draftScope]),
  );
  const [query, setQuery] = useState("");
  const [collapsed, setCollapsed] = useState<ReadonlySet<string>>(new Set());
  const [pickerOpen, setPickerOpen] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  // A native menu closes on selection, so a failed star/mute lands here, above
  // the rows — the room screen does the same with its header buttons.
  const [prefsError, setPrefsError] = useState<string | null>(null);

  // The search field and the compose button live in the (app) Stack's header,
  // which all four tabs share, so the list writes them there — keyed on the
  // tab bar's SELECTION, not on focus. A room pushed over the tabs blurs this
  // screen too, and stripping the header then would pull the search bar out
  // of the outgoing nav bar mid-push and drop it back in at the end of a
  // swipe back. Leaving the tab cancels the search, so the field is empty
  // whichever tab takes the header next.
  const header = useNavigation<NativeStackNavigationProp<ParamListBase>>("/(app)");
  const { key } = useRoute();
  const selected = useNavigationState((state) => state.routes[state.index]?.key === key);
  const searchBar = useRef<SearchBarCommands>(null);
  useLayoutEffect(() => {
    if (!selected) return;
    header.setOptions({
      headerSearchBarOptions: {
        ref: searchBar,
        placeholder: "Search",
        autoCapitalize: "none",
        // In the nav bar, under the title — not folded into a toolbar or the
        // tab bar the way iOS 26 otherwise may.
        placement: "stacked",
        // UIKit hides it against ONE scroll view per stack screen, and all four
        // tabs are one stack screen: on DMs it would follow Home's list.
        hideWhenScrolling: false,
        onChangeText: (event) => setQuery(event.nativeEvent.text),
        onCancelButtonPress: () => setQuery(""),
      },
      headerRight: () => <ComposeButton onPress={() => setPickerOpen(true)} />,
    });
    return () => {
      // eslint-disable-next-line react-hooks/exhaustive-deps -- the bar mounts after this effect runs; only the cleanup-time ref reaches it
      searchBar.current?.cancelSearch();
      setQuery("");
      header.setOptions({ headerSearchBarOptions: undefined, headerRight: undefined });
    };
  }, [header, selected]);

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

  const refresh = () => {
    setRefreshing(true);
    void rooms.refreshRooms().finally(() => setRefreshing(false));
  };

  const savePrefs = (roomId: string, prefs: MemberPrefsRequest, failure: string) => {
    setPrefsError(null);
    void rooms.setRoomPrefs(roomId, prefs).catch(() => setPrefsError(failure));
  };
  // One handler for every row, from its menu, its swipes and VoiceOver alike.
  // A failed read-state change shows as the badge not changing, as on the room
  // screen's own Mark unread.
  const onRowAction = (room: RoomSummary, action: RoomRowAction) => {
    switch (action) {
      case "star":
        savePrefs(room.roomId, { starred: !room.starred }, STAR_FAILED);
        break;
      case "mute":
        savePrefs(room.roomId, { muted: !room.muted }, MUTE_FAILED);
        break;
      case "markUnread":
        void rooms.markUnread(room.roomId).catch(() => {});
        break;
      case "markRead":
        // The room machine only marks the room it has open, and the list opens
        // none — so straight to the cursor, then the list for the new count.
        void messaging
          .markRead(room.roomId, { lastReadSeq: room.lastMessageSeq })
          .then(() => rooms.refreshRooms())
          .catch(() => {});
        break;
      case "copyLink":
        void Clipboard.setStringAsync(roomUrl(room.roomId));
        break;
    }
  };

  return (
    <View style={styles.page}>
      <FlatList
        testID="room-list"
        contentInsetAdjustmentBehavior="automatic"
        automaticallyAdjustKeyboardInsets
        data={data}
        keyExtractor={(row) => (row.kind === "header" ? row.title : row.room.roomId)}
        contentContainerStyle={[styles.list, data.length === 0 && styles.listGrow]}
        keyboardShouldPersistTaps="handled"
        refreshing={refreshing}
        onRefresh={refresh}
        // A hard failure over a cached list reports itself ABOVE the rows
        // rather than replacing them — the rooms we already have are still the
        // truest thing on screen (same idiom the retired roster used).
        ListHeaderComponent={
          <>
            {prefsError && (
              <Text accessibilityRole="alert" style={styles.prefsError}>
                {prefsError}
              </Text>
            )}
            {rooms.roomsError ? (
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
            ) : null}
          </>
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
            <RoomListRow
              room={item.room}
              participants={participants(item.room)}
              hasDraft={roomsWithDrafts.has(item.room.roomId)}
              onAction={onRowAction}
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
    </View>
  );
}

export default function Home() {
  return <RoomsScreen />;
}

const styles = StyleSheet.create({
  page: { flex: 1, backgroundColor: colors.cream },
  compose: {
    width: 36,
    height: 36,
    borderRadius: radii.sm,
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

  prefsError: {
    fontFamily: fonts.sans,
    fontSize: 13,
    lineHeight: 18,
    color: colors.errorText,
    paddingHorizontal: spacing.lg,
    paddingTop: spacing.sm,
  },
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
