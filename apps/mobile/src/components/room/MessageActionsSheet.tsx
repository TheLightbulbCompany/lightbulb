import Feather from "@expo/vector-icons/Feather";
import type { CanonicalRoomMessage } from "@lightbulb/shared";
import * as Clipboard from "expo-clipboard";
import type { ComponentProps } from "react";
import { Pressable, StyleSheet, Text, View } from "react-native";

import { colors, fonts, radii, spacing } from "../../config/theme";
import { hapticSelection } from "../../lib/haptics";
import { messageUrl } from "../../lib/webLinks";
import { BottomSheet } from "../ui/BottomSheet";

/** The shipped fixed set, the same four the web's RoomPanel defaults to. */
export const QUICK_REACTIONS = ["👍", "✅", "🎉", "👀"] as const;

interface ActionRow {
  icon: ComponentProps<typeof Feather>["name"];
  label: string;
  run: () => void | Promise<unknown>;
}

/**
 * The long-press sheet: the fixed reaction bar over Reply in thread / Copy text
 * / Mark unread / Copy link.
 *
 * Kept mounted by its callers and toggled via `visible` (BottomSheet's own
 * contract), so closing — from a row action, the scrim, or a drag — always
 * plays the sheet's spring-out before `onClose` runs.
 *
 * `onReplyInThread` and `onMarkUnread` are optional and their rows are omitted
 * without them — the same `!inThread` gate the web applies to both. Inside a
 * thread there is nowhere to reply-in-thread TO (the composer below already is
 * that thread), and the read cursor is per room, so a thread cannot express its
 * own unread.
 *
 * ponytail: fixed set only; the "more" picker lands with Phase B's emoji sheet,
 * which also brings Edit and Delete for your own messages.
 */
export function MessageActionsSheet({
  message,
  currentUserId,
  roomId,
  visible = true,
  onClose,
  onReact,
  onReplyInThread,
  onMarkUnread,
}: {
  message: CanonicalRoomMessage;
  currentUserId: string;
  roomId: string;
  /**
   * Defaults to `true` for backward compatibility — a caller that still
   * conditionally mounts this component (the old `{target && <.../>}` shape)
   * gets its old always-open behaviour. A caller that keeps it mounted and
   * toggles this instead gets BottomSheet's spring open/close.
   */
  visible?: boolean;
  onClose: () => void;
  onReact: (emoji: string, active: boolean) => void;
  onReplyInThread?: () => void;
  onMarkUnread?: () => void;
}) {
  // A tombstone carries no text, no attachments and no reactions (the server
  // blanks all three), so reacting to one and copying its text are both dead.
  const deleted = Boolean(message.deletedAt);
  const rows: ActionRow[] = [
    ...(onReplyInThread
      ? [{ icon: "corner-up-left" as const, label: "Reply in thread", run: onReplyInThread }]
      : []),
    ...(deleted
      ? []
      : [
          {
            icon: "copy" as const,
            label: "Copy text",
            run: () => Clipboard.setStringAsync(message.text),
          },
        ]),
    ...(onMarkUnread ? [{ icon: "mail" as const, label: "Mark unread", run: onMarkUnread }] : []),
    {
      icon: "link" as const,
      label: "Copy link",
      run: () => Clipboard.setStringAsync(messageUrl(roomId, message.messageId)),
    },
  ];

  return (
    <BottomSheet visible={visible} onClose={onClose} closeLabel="Close message actions">
      <View style={styles.card}>
        <View style={styles.handle} />
        {deleted ? null : (
          <View style={styles.reactions}>
            {QUICK_REACTIONS.map((emoji) => {
              const active = message.reactions?.[emoji]?.includes(currentUserId) ?? false;
              return (
                <Pressable
                  key={emoji}
                  onPress={() => {
                    hapticSelection();
                    onReact(emoji, !active);
                    onClose();
                  }}
                  accessibilityRole="button"
                  accessibilityLabel={active ? `Remove ${emoji} reaction` : `React with ${emoji}`}
                  accessibilityState={{ selected: active }}
                  style={({ pressed }) => [
                    styles.reaction,
                    active && styles.reactionActive,
                    pressed && styles.reactionPressed,
                  ]}
                >
                  <Text style={styles.emoji}>{emoji}</Text>
                </Pressable>
              );
            })}
          </View>
        )}
        {rows.map(({ icon, label, run }) => (
          <Pressable
            key={label}
            onPress={() => {
              void run();
              onClose();
            }}
            accessibilityRole="button"
            style={({ pressed }) => [styles.row, pressed && styles.rowPressed]}
          >
            <Feather name={icon} size={16} color={colors.inkSoft} />
            <Text style={styles.rowLabel}>{label}</Text>
          </Pressable>
        ))}
      </View>
    </BottomSheet>
  );
}

const styles = StyleSheet.create({
  card: {
    backgroundColor: colors.cream,
    borderTopLeftRadius: radii.xl,
    borderTopRightRadius: radii.xl,
    paddingBottom: spacing.md,
  },
  handle: {
    alignSelf: "center",
    width: 36,
    height: 4,
    borderRadius: 2,
    backgroundColor: colors.edge,
    marginTop: spacing.sm,
  },
  reactions: {
    flexDirection: "row",
    gap: spacing.md,
    paddingHorizontal: spacing.lg,
    paddingTop: spacing.lg,
    paddingBottom: spacing.sm,
  },
  reaction: {
    width: 44,
    height: 44,
    borderRadius: 22,
    borderWidth: 1,
    borderColor: colors.edge,
    alignItems: "center",
    justifyContent: "center",
  },
  // The manual's rule: state is a border and a tint, never a loud fill.
  reactionActive: { backgroundColor: colors.forestTint, borderColor: colors.forest },
  reactionPressed: { backgroundColor: colors.rowPressed },
  emoji: { fontSize: 20, lineHeight: 24 },
  row: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.md,
    paddingHorizontal: spacing.lg,
    paddingVertical: spacing.md,
  },
  rowPressed: { backgroundColor: colors.rowPressed },
  rowLabel: { fontFamily: fonts.sansMedium, fontSize: 15, lineHeight: 20, color: colors.ink },
});
