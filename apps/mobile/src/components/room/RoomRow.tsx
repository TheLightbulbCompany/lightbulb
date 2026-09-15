import { roomDisplayName, type RoomMember, type RoomSummary } from "@lightbulb/shared";
import { Pressable, StyleSheet, Text, View } from "react-native";

import { Avatar } from "./Avatar";
import { colors, fonts, spacing, type } from "../../config/theme";
import { relativeTime } from "../../lib/relativeTime";

/**
 * Everything a row needs of a member: the name it shows, the tile it draws,
 * and (for a human) whether they're online. `online` isn't on `RoomMember`
 * yet -- mobile has no presence data for humans, so it stays undefined until
 * that field reaches the app; see Avatar.tsx.
 */
export type RowParticipant = Pick<RoomMember, "kind" | "displayName"> & { online?: boolean };

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
  onLongPress,
}: {
  room: RoomSummary;
  participants: readonly RowParticipant[];
  unread: number;
  muted: boolean;
  onPress: () => void;
  /** Long press opens the conversation actions sheet (star/mute/mark unread/copy link). */
  onLongPress?: () => void;
}) {
  const label = roomDisplayName(room, participants);
  const count = muted ? 0 : unread;
  const isChannel = room.kind === "channel";
  return (
    <Pressable
      testID={`room-row-${room.roomId}`}
      onPress={onPress}
      onLongPress={onLongPress}
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
          <Avatar
            name={label}
            kind={participants[0]?.kind ?? "human"}
            online={participants[0]?.online}
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
});
