import Feather from "@expo/vector-icons/Feather";
import type { MemberPrefsRequest, RoomSummary } from "@lightbulb/shared";
import * as Clipboard from "expo-clipboard";
import { useState, type ComponentProps } from "react";
import { Pressable, StyleSheet, Text, View } from "react-native";

import { colors, fonts, radii, spacing } from "../../config/theme";
import { roomUrl } from "../../lib/webLinks";
import { BottomSheet } from "../ui/BottomSheet";

interface ActionRow {
  icon: ComponentProps<typeof Feather>["name"];
  label: string;
  run: () => void;
}

/** RoomHeader.tsx's own copy — the same two strings both surfaces show. */
const STAR_FAILED = "The star could not be saved.";
const MUTE_FAILED = "The notification setting could not be saved.";

/**
 * The room list's long-press sheet: star/mute/mark-unread/copy-link, in
 * RoomDirectoryRow's order and wording. Leave channel stays out for now (task
 * 6 brief) — that needs a confirm dialog this sheet doesn't have yet.
 *
 * Mirrors MessageActionsSheet's shape: kept mounted by the caller and toggled
 * via `visible` (BottomSheet's own contract), so closing always plays the
 * sheet's spring-out before `onClose` runs. Unlike Mark unread
 * (fire-and-forget, same as the message sheet elsewhere in this app),
 * Star/Mute keep the sheet open on failure and show the web's own copy
 * inline — there is nowhere else on the room list for that error to land,
 * and this sheet has the room a native header doesn't.
 */
export function RoomActionsSheet({
  room,
  visible = true,
  onClose,
  onSetPrefs,
  onMarkUnread,
}: {
  room: RoomSummary;
  /** See MessageActionsSheet's `visible` — same default-true contract. */
  visible?: boolean;
  onClose: () => void;
  onSetPrefs: (prefs: MemberPrefsRequest) => Promise<unknown>;
  onMarkUnread: () => void | Promise<unknown>;
}) {
  const [error, setError] = useState<string | null>(null);

  async function savePrefs(prefs: MemberPrefsRequest, failureMessage: string) {
    setError(null);
    try {
      await onSetPrefs(prefs);
      onClose();
    } catch {
      setError(failureMessage);
    }
  }

  function runAndClose(action: () => void | Promise<unknown>) {
    void action();
    onClose();
  }

  const rows: ActionRow[] = [
    {
      icon: "star",
      label: room.starred ? "Unstar conversation" : "Star conversation",
      run: () => void savePrefs({ starred: !room.starred }, STAR_FAILED),
    },
    {
      icon: room.muted ? "bell" : "bell-off",
      label: room.muted ? "Unmute conversation" : "Mute conversation",
      run: () => void savePrefs({ muted: !room.muted }, MUTE_FAILED),
    },
    {
      icon: "mail",
      label: "Mark unread",
      run: () => runAndClose(onMarkUnread),
    },
    {
      icon: "link",
      label: "Copy link",
      run: () => runAndClose(() => Clipboard.setStringAsync(roomUrl(room.roomId))),
    },
  ];

  return (
    <BottomSheet visible={visible} onClose={onClose} closeLabel="Close conversation actions">
      <View style={styles.card}>
        <View style={styles.handle} />
        {rows.map(({ icon, label, run }) => (
          <Pressable
            key={label}
            onPress={run}
            accessibilityRole="button"
            style={({ pressed }) => [styles.row, pressed && styles.rowPressed]}
          >
            <Feather name={icon} size={16} color={colors.inkSoft} />
            <Text style={styles.rowLabel}>{label}</Text>
          </Pressable>
        ))}
        {error && (
          <Text accessibilityRole="alert" style={styles.error}>
            {error}
          </Text>
        )}
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
  row: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.md,
    paddingHorizontal: spacing.lg,
    paddingVertical: spacing.md,
  },
  rowPressed: { backgroundColor: colors.rowPressed },
  rowLabel: { fontFamily: fonts.sansMedium, fontSize: 15, lineHeight: 20, color: colors.ink },
  error: {
    fontFamily: fonts.sans,
    fontSize: 13,
    lineHeight: 18,
    color: colors.errorText,
    paddingHorizontal: spacing.lg,
    paddingTop: spacing.xs,
  },
});
