import { SymbolView, type SFSymbol } from "../ui/SymbolView";
import type { CanonicalRoomMessage } from "@lightbulb/shared";
import * as Clipboard from "expo-clipboard";
import type { ComponentProps } from "react";
import { Alert, Linking, Pressable, StyleSheet, Text, View } from "react-native";

import { fonts, radii, spacing, useChatPalette } from "../../config/theme";
import { hapticSelection } from "../../lib/haptics";
import { messageUrl } from "../../lib/webLinks";
import { BottomSheet } from "../ui/BottomSheet";

/** The shipped fixed set, the same four the web's RoomPanel defaults to. */
export const QUICK_REACTIONS = ["👍", "✅", "🎉", "👀"] as const;

/**
 * App Store 1.2: a prefilled email to support, which needs no backend. The
 * ids are what support looks the message up by; the excerpt is for the human.
 */
export function reportMessageUrl(roomId: string, message: CanonicalRoomMessage): string {
  // Cut on code points: a UTF-16 slice can split an emoji's surrogate pair,
  // and encodeURIComponent throws on a lone surrogate.
  const chars = Array.from(message.text);
  const excerpt = chars.length > 280 ? `${chars.slice(0, 280).join("")}…` : message.text;
  const body = [
    "I want to report this message.",
    "",
    `Room: ${roomId}`,
    `Message: ${message.messageId}`,
    `Author: ${message.author.displayName} (${message.author.kind} ${message.author.id})`,
    `Excerpt: ${excerpt}`,
  ].join("\n");
  return `mailto:support@thelightbulb.company?subject=${encodeURIComponent("Report a message")}&body=${encodeURIComponent(body)}`;
}

interface ActionRow {
  icon: SFSymbol;
  label: string;
  run: () => void | Promise<unknown>;
}

/**
 * The long-press sheet: the fixed reaction bar over Reply in thread / Copy text
 * / Mark unread / Copy link / Report (not on your own messages).
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
  const palette = useChatPalette();
  // A tombstone carries no text, no attachments and no reactions (the server
  // blanks all three), so reacting to one and copying its text are both dead.
  const deleted = Boolean(message.deletedAt);
  const rows: ActionRow[] = [
    ...(onReplyInThread
      ? [{ icon: "arrowshape.turn.up.left" as const, label: "Reply in thread", run: onReplyInThread }]
      : []),
    ...(deleted
      ? []
      : [
          {
            icon: "doc.on.doc" as const,
            label: "Copy text",
            run: () => Clipboard.setStringAsync(message.text),
          },
        ]),
    ...(onMarkUnread ? [{ icon: "envelope" as const, label: "Mark unread", run: onMarkUnread }] : []),
    {
      icon: "link" as const,
      label: "Copy link",
      run: () => Clipboard.setStringAsync(messageUrl(roomId, message.messageId)),
    },
    ...(message.author.kind === "human" && message.author.id === currentUserId
      ? []
      : [
          {
            icon: "flag" as const,
            label: "Report",
            // No mail app configured: openURL rejects, so say where to write instead.
            run: () =>
              Linking.openURL(reportMessageUrl(roomId, message)).catch(() =>
                Alert.alert("Report this message", "Email support@thelightbulb.company."),
              ),
          },
        ]),
  ];

  return (
    <BottomSheet visible={visible} onClose={onClose}>
      <View style={[styles.card, { backgroundColor: palette.surface }]}>
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
                    { borderColor: palette.edge },
                    // The manual's rule: state is a border and a tint, never a loud fill.
                    active && { backgroundColor: palette.chip, borderColor: palette.fg },
                    pressed && { backgroundColor: palette.hover },
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
            style={({ pressed }) => [styles.row, pressed && { backgroundColor: palette.hover }]}
          >
            <SymbolView accessibilityElementsHidden name={icon} size={16} tintColor={palette.fgSoft} />
            <Text style={[styles.rowLabel, { color: palette.fg }]}>{label}</Text>
          </Pressable>
        ))}
      </View>
    </BottomSheet>
  );
}

const styles = StyleSheet.create({
  card: {
    borderTopLeftRadius: radii.xl,
    borderTopRightRadius: radii.xl,
    paddingBottom: spacing.md,
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
    alignItems: "center",
    justifyContent: "center",
  },
  emoji: { fontSize: 20, lineHeight: 24 },
  row: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.md,
    paddingHorizontal: spacing.lg,
    paddingVertical: spacing.md,
  },
  rowLabel: { fontFamily: fonts.sansMedium, fontSize: 15, lineHeight: 20 },
});
