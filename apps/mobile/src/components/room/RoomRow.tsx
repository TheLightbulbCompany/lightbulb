import { roomDisplayName, type RoomDetail, type RoomMember, type RoomSummary } from "@lightbulb/shared";
import { Pressable, StyleSheet, Text, View } from "react-native";

import { Avatar } from "./Avatar";
import { colors, fonts, radii, spacing, type } from "../../config/theme";
import { relativeTime } from "../../lib/relativeTime";

/** Everything a row needs of a member: the name it shows, the tile it draws. */
export type RowParticipant = Pick<RoomMember, "kind" | "displayName">;

/**
 * The room's members minus the viewer — a DM is named after the OTHER party,
 * and a group DM after the others in order.
 */
export function otherMembers(
  detail: RoomDetail | undefined,
  userId: string | null | undefined,
): RowParticipant[] {
  return (detail?.members ?? []).filter(
    (member) => !(member.kind === "human" && member.id === userId),
  );
}

/**
 * What the row shows and what search matches on. `#`-prefixed for channels,
 * exactly as the conversation screen's header does it.
 *
 * ponytail: every channel gets `#`; swap in a lock glyph when RoomSummary
 * carries a private flag.
 */
export function roomLabel(
  room: Pick<RoomSummary, "kind" | "name">,
  participants: readonly RowParticipant[],
): string {
  return `${room.kind === "channel" ? "#" : ""}${roomDisplayName(room, participants)}`;
}

/** Newest activity first — a room with no message yet falls back to updatedAt. */
export function byRecency(left: RoomSummary, right: RoomSummary): number {
  const at = (room: RoomSummary) => Date.parse(room.lastMessageAt ?? room.updatedAt) || 0;
  return at(right) - at(left);
}

/**
 * One Home/DMs list row, Slack's anatomy: leading `#` or avatar(s), the name
 * (bold while unread), the last-activity time, and an unread pill on direct
 * conversations. A muted room dims and never carries a badge, so it never
 * shouts from the list either.
 */
export function RoomRow({
  room,
  participants,
  unread,
  muted,
  onPress,
}: {
  room: RoomSummary;
  participants: readonly RowParticipant[];
  unread: number;
  muted: boolean;
  onPress: () => void;
}) {
  const label = roomLabel(room, participants);
  const count = muted ? 0 : unread;
  const isChannel = room.kind === "channel";
  return (
    <Pressable
      testID={`room-row-${room.roomId}`}
      onPress={onPress}
      accessibilityRole="button"
      accessibilityLabel={count > 0 ? `${label}, ${count} unread` : label}
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
          <Avatar name={roomDisplayName(room, participants)} kind={participants[0]?.kind ?? "human"} />
        </View>
      )}
      <Text style={[styles.label, count > 0 && styles.labelUnread]} numberOfLines={1}>
        {label}
      </Text>
      <Text style={styles.time}>{relativeTime(Date.parse(room.lastMessageAt ?? ""))}</Text>
      {!isChannel && count > 0 ? (
        <View style={styles.badge}>
          <Text style={styles.badgeLabel}>{count > 99 ? "99+" : count}</Text>
        </View>
      ) : null}
    </Pressable>
  );
}

const styles = StyleSheet.create({
  row: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.md,
    paddingHorizontal: spacing.lg,
    paddingVertical: spacing.sm,
  },
  rowPressed: { backgroundColor: colors.rowPressed },
  rowMuted: { opacity: 0.55 },
  lead: { width: 28, alignItems: "center", justifyContent: "center" },
  stack: { flexDirection: "row" },
  stackSecond: { marginLeft: -8 },
  hash: { fontFamily: fonts.sansMedium, fontSize: 18, color: colors.mute },
  label: { ...type.body, flex: 1, color: colors.inkSoft },
  labelUnread: { fontFamily: fonts.sansSemiBold, color: colors.ink },
  time: { ...type.caption },
  badge: {
    minWidth: 20,
    paddingHorizontal: spacing.xs,
    paddingVertical: 1,
    borderRadius: radii.sm,
    backgroundColor: colors.forest,
    alignItems: "center",
  },
  badgeLabel: {
    fontFamily: fonts.sansSemiBold,
    fontSize: 11,
    lineHeight: 16,
    color: colors.cream,
  },
});
