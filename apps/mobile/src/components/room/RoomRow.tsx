import { roomDisplayName, type RoomMember, type RoomSummary } from "@lightbulb/shared";
import { Link } from "expo-router";
import { SymbolView, type SFSymbol } from "expo-symbols";
import {
  Pressable,
  StyleSheet,
  Text,
  View,
  type PressableProps,
  type ViewStyle,
} from "react-native";
import ReanimatedSwipeable, {
  type SwipeableMethods,
} from "react-native-gesture-handler/ReanimatedSwipeable";

import { Avatar } from "./Avatar";
import { colors, fonts, spacing, type } from "../../config/theme";
import { hapticSelection } from "../../lib/haptics";
import { relativeTime } from "../../lib/relativeTime";

/**
 * Everything a row needs of a member: the name it shows, the tile it draws,
 * and (for a human) whether they're online or sharing a live terminal
 * session. `online` isn't on `RoomMember` yet -- mobile has no presence data
 * for humans, so it stays undefined until that field reaches the app; see
 * Avatar.tsx. `id` is kept so a caller can look a member up against a
 * sharing set without re-deriving the list from `RoomMember[]`.
 */
export type RowParticipant = Pick<RoomMember, "kind" | "displayName" | "id"> & {
  online?: boolean;
  sharing?: boolean;
};

/**
 * The room's members minus the viewer — a DM is named after the OTHER party,
 * and a group DM after the others in order.
 */
export function otherMembers(
  detail: RoomSummary | undefined,
  userId: string | null | undefined,
): RowParticipant[] {
  return (detail?.members ?? []).filter(
    (member) => !(member.kind === "human" && member.id === userId),
  );
}

/** Newest activity first — a room with no message yet falls back to updatedAt. */
export function byRecency(left: RoomSummary, right: RoomSummary): number {
  const at = (room: RoomSummary) => Date.parse(room.lastMessageAt ?? room.updatedAt) || 0;
  return at(right) - at(left);
}

/**
 * One Home/DMs list row, Slack's anatomy: a leading glyph COLUMN (`#`, or the
 * avatar(s)), then the bare name — the `#` is never also a prefix on the label,
 * or the row reads `# #launch`. The conversation screen's header is the other
 * convention on purpose: one `#name` string, because it has no glyph column.
 *
 * The name goes bold while unread, the last-activity time trails it, and direct
 * conversations carry an unread pill. A muted room dims and never carries a
 * badge, so it never shouts from the list either.
 *
 * ponytail: every channel gets `#`; swap in a lock glyph when RoomSummary
 * carries a private flag.
 */
export function RoomRow({
  room,
  participants,
  unread,
  muted,
  onPress,
  accessibilityActions,
  onAccessibilityAction,
}: {
  room: RoomSummary;
  participants: readonly RowParticipant[];
  unread: number;
  muted: boolean;
  /** Supplied by `RoomListRow`'s `Link.Trigger`, which owns navigation. */
  onPress?: () => void;
  accessibilityActions?: PressableProps["accessibilityActions"];
  onAccessibilityAction?: PressableProps["onAccessibilityAction"];
}) {
  const label = roomDisplayName(room, participants);
  const count = muted ? 0 : unread;
  const isChannel = room.kind === "channel";
  // The badge on the DM's avatar is invisible to VoiceOver -- the row's own
  // label is the only thing it reads, and it otherwise says nothing about it.
  const sharingSuffix = room.kind === "dm" && participants[0]?.sharing ? ", sharing a terminal session" : "";
  return (
    <Pressable
      testID={`room-row-${room.roomId}`}
      onPress={onPress}
      accessibilityActions={accessibilityActions}
      onAccessibilityAction={onAccessibilityAction}
      accessibilityRole="button"
      accessibilityLabel={`${count > 0 ? `${label}, ${count} unread` : label}${sharingSuffix}`}
      style={({ pressed }) => [
        styles.row,
        muted && styles.rowMuted,
        pressed && styles.rowPressed,
      ]}
    >
      {isChannel ? (
        <View style={styles.lead}>
          <Text style={styles.hash}>#</Text>
        </View>
      ) : room.kind === "group-dm" ? (
        <View style={[styles.lead, styles.stack]}>
          <Avatar name={participants[0]?.displayName ?? "?"} kind={participants[0]?.kind ?? "human"} size={20} />
          <View style={styles.stackSecond}>
            <Avatar name={participants[1]?.displayName ?? "?"} kind={participants[1]?.kind ?? "human"} size={20} />
          </View>
        </View>
      ) : (
        <View style={styles.lead}>
          <Avatar
            name={label}
            kind={participants[0]?.kind ?? "human"}
            online={participants[0]?.online}
            sharing={participants[0]?.sharing}
            showPresence
          />
        </View>
      )}
      <Text style={[styles.label, count > 0 && styles.labelUnread]} numberOfLines={1}>
        {label}
      </Text>
      {/* Colour (the dimmed row) is never the only signal — the word too. */}
      {muted && <Text style={styles.mutedTag}>Muted</Text>}
      {room.shared && <Text style={styles.mutedTag}>Shared</Text>}
      <Text style={styles.time}>{relativeTime(Date.parse(room.lastMessageAt ?? ""))}</Text>
      {!isChannel && count > 0 ? (
        <View style={styles.badge}>
          <Text style={styles.badgeLabel}>{count > 99 ? "99+" : count}</Text>
        </View>
      ) : null}
    </Pressable>
  );
}

/** Everything a list row can do to its conversation besides open it. */
export type RoomRowAction = "star" | "mute" | "markRead" | "markUnread" | "copyLink";

/**
 * One full-height swipe button, iOS Mail's anatomy: glyph over a short label.
 * Hidden from VoiceOver, which cannot swipe — the row carries the same
 * actions as accessibility actions instead, which is how UIKit exposes
 * swipe actions.
 */
function SwipeAction({
  icon,
  label,
  accessibilityLabel,
  tone,
  onPress,
}: {
  icon: SFSymbol;
  label: string;
  accessibilityLabel: string;
  tone: ViewStyle;
  onPress: () => void;
}) {
  return (
    <Pressable
      onPress={onPress}
      accessibilityRole="button"
      accessibilityLabel={accessibilityLabel}
      accessibilityElementsHidden
      importantForAccessibility="no-hide-descendants"
      style={({ pressed }) => [styles.swipeAction, tone, pressed && styles.swipePressed]}
    >
      <SymbolView accessibilityElementsHidden name={icon} size={20} tintColor={colors.white} />
      <Text style={styles.swipeLabel}>{label}</Text>
    </Pressable>
  );
}

/**
 * A Home/DMs row that behaves like an iOS list row: tap opens the conversation,
 * a long press lifts a live preview of it with a native context menu (the
 * retired long-press sheet's four actions, in its wording), a leading swipe
 * marks it read or unread and a trailing swipe mutes it.
 *
 * The row hands itself back through one `onAction` rather than owning any of
 * it: the list keeps the room machine, and the failure copy has to land on the
 * list, since a native menu closes on selection and has nowhere to show it.
 *
 * ponytail: a tap on the revealed button commits a swipe action. iOS also
 * commits on a full-width swipe (needs the translation watched on the UI
 * thread) and keeps one row open at a time (needs the list to hold the open
 * row) — add them if the swipes get used.
 */
export function RoomListRow({
  room,
  participants,
  onAction,
}: {
  room: RoomSummary;
  participants: readonly RowParticipant[];
  onAction: (room: RoomSummary, action: RoomRowAction) => void;
}) {
  const unread = room.unreadCount > 0;
  const readAction: RoomRowAction = unread ? "markRead" : "markUnread";
  const readLabel = unread ? "Mark read" : "Mark unread";
  const starLabel = room.starred ? "Unstar conversation" : "Star conversation";
  const muteLabel = room.muted ? "Unmute conversation" : "Mute conversation";
  // Close first, so the row is already springing shut as its state changes.
  const commit = (swipe: SwipeableMethods, action: RoomRowAction) => {
    swipe.close();
    hapticSelection();
    onAction(room, action);
  };
  return (
    <ReanimatedSwipeable
      // Stops at the button's width: past it, the row would pull away from
      // the button and open an uncoloured gap between them.
      overshootLeft={false}
      overshootRight={false}
      childrenContainerStyle={styles.surface}
      renderLeftActions={(_progress, _translation, swipe) => (
        <SwipeAction
          icon={unread ? "envelope.open.fill" : "envelope.badge.fill"}
          label={unread ? "Read" : "Unread"}
          accessibilityLabel={readLabel}
          tone={styles.swipeRead}
          onPress={() => commit(swipe, readAction)}
        />
      )}
      renderRightActions={(_progress, _translation, swipe) => (
        <SwipeAction
          icon={room.muted ? "bell.fill" : "bell.slash.fill"}
          label={room.muted ? "Unmute" : "Mute"}
          accessibilityLabel={muteLabel}
          tone={styles.swipeMute}
          onPress={() => commit(swipe, "mute")}
        />
      )}
    >
      <Link href={`/(app)/room/${room.roomId}`} asChild>
        <Link.Trigger>
          <RoomRow
            room={room}
            participants={participants}
            unread={room.unreadCount}
            muted={room.muted}
            accessibilityActions={[
              { name: readAction, label: readLabel },
              { name: "mute", label: muteLabel },
              { name: "star", label: starLabel },
              { name: "copyLink", label: "Copy link" },
            ]}
            onAccessibilityAction={(event) =>
              onAction(room, event.nativeEvent.actionName as RoomRowAction)
            }
          />
        </Link.Trigger>
        {/* The conversation itself, not a snapshot: the room screen renders
            read-only in a peek (`useIsPreview`), so it marks nothing read. */}
        <Link.Preview />
        <Link.Menu>
          <Link.MenuAction
            icon={room.starred ? "star.slash" : "star"}
            onPress={() => onAction(room, "star")}
          >
            {starLabel}
          </Link.MenuAction>
          <Link.MenuAction
            icon={room.muted ? "bell" : "bell.slash"}
            onPress={() => onAction(room, "mute")}
          >
            {muteLabel}
          </Link.MenuAction>
          <Link.MenuAction icon="envelope.badge" onPress={() => onAction(room, "markUnread")}>
            Mark unread
          </Link.MenuAction>
          <Link.MenuAction icon="link" onPress={() => onAction(room, "copyLink")}>
            Copy link
          </Link.MenuAction>
        </Link.Menu>
      </Link>
    </ReanimatedSwipeable>
  );
}

const styles = StyleSheet.create({
  row: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.sm,
    minHeight: 44,
    paddingHorizontal: spacing.lg,
    paddingVertical: spacing.sm,
  },
  rowPressed: { backgroundColor: colors.rowPressed },
  rowMuted: { opacity: 0.55 },
  lead: { width: 28, alignItems: "center", justifyContent: "center" },
  stack: { flexDirection: "row" },
  stackSecond: { marginLeft: -8 },
  hash: { fontFamily: fonts.sansMedium, fontSize: 18, color: colors.mute },
  label: { fontFamily: fonts.sans, fontSize: 14, lineHeight: 20, flex: 1, color: colors.inkSoft },
  labelUnread: { fontFamily: fonts.sansSemiBold, color: colors.ink },
  mutedTag: { ...type.caption },
  time: { ...type.caption },
  badge: {
    minWidth: 18,
    paddingHorizontal: 6,
    paddingVertical: 1,
    borderRadius: 999,
    backgroundColor: colors.info,
    alignItems: "center",
  },
  badgeLabel: {
    fontFamily: fonts.sansSemiBold,
    fontSize: 11,
    lineHeight: 18,
    color: colors.cream,
  },
  // Opaque, so the row covers the actions behind it until it slides aside.
  surface: { backgroundColor: colors.cream },
  swipeAction: {
    width: 80,
    alignItems: "center",
    justifyContent: "center",
    gap: spacing.xs,
  },
  swipePressed: { opacity: 0.8 },
  // The unread badge's own blue, so the action reads as the badge it toggles.
  swipeRead: { backgroundColor: colors.info },
  swipeMute: { backgroundColor: colors.forest },
  swipeLabel: { fontFamily: fonts.sansMedium, fontSize: 13, lineHeight: 16, color: colors.white },
});
