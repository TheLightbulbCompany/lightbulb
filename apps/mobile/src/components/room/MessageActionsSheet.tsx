import { SymbolView, type SFSymbol } from "../ui/SymbolView";
import type { CanonicalRoomMessage } from "@lightbulb/shared";
import * as Clipboard from "expo-clipboard";
import type { ComponentProps } from "react";
import { Alert, Linking, Pressable, StyleSheet, Text, View } from "react-native";

import { fonts, radii, spacing, useChatPalette } from "../../config/theme";
import { messageUrl } from "../../lib/webLinks";
import { BottomSheet } from "../ui/BottomSheet";

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
 * The long-press sheet: Copy text / Mark unread / Copy link / Report (not on
 * your own messages). Kept mounted by its callers and toggled via `visible`.
 */
export function MessageActionsSheet({
  message,
  currentUserId,
  roomId,
  visible = true,
  onClose,
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
  onMarkUnread?: () => void;
}) {
  const palette = useChatPalette();
  // A tombstone carries no text (the server blanks it), so copying it is dead.
  const deleted = Boolean(message.deletedAt);
  const rows: ActionRow[] = [
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
  row: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.md,
    paddingHorizontal: spacing.lg,
    paddingVertical: spacing.md,
  },
  rowLabel: { fontFamily: fonts.sansMedium, fontSize: 15, lineHeight: 20 },
});
