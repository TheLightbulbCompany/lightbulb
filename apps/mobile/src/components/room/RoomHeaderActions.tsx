import Feather from "@expo/vector-icons/Feather";
import { Pressable, StyleSheet, Text, View } from "react-native";

import { colors, fonts, radii } from "../../config/theme";

/**
 * The conversation header's star/mute icon pair — RoomHeader.tsx's own icon
 * buttons, same `aria-label` wording as `accessibilityLabel`. Presentational
 * only: a native header has no room below the buttons for an inline error, so
 * the screen that mounts this owns the async call, the pending flag and the
 * failure copy, and shows it in the screen body instead.
 */
export function RoomHeaderActions({
  starred,
  muted,
  disabled,
  onToggleStar,
  onToggleMute,
  onOpenDevices,
  liveSessions = 0,
}: {
  starred: boolean;
  muted: boolean;
  disabled?: boolean;
  onToggleStar: () => void;
  onToggleMute: () => void;
  /** Present only on rooms that can show shared devices & sessions. */
  onOpenDevices?: () => void;
  /** Sessions working or waiting for approval; drawn as a badge. */
  liveSessions?: number;
}) {
  return (
    <View style={styles.row}>
      {onOpenDevices && (
        <Pressable
          onPress={onOpenDevices}
          disabled={disabled}
          hitSlop={8}
          accessibilityRole="button"
          accessibilityLabel={liveSessions > 0 ? `Devices & sessions, ${liveSessions} live` : "Devices & sessions"}
          accessibilityState={{ disabled: !!disabled }}
          style={({ pressed }) => [styles.button, pressed && styles.buttonPressed]}
        >
          <Feather name="terminal" size={18} color={colors.inkSoft} />
          {liveSessions > 0 && (
            <View style={styles.badge}>
              <Text style={styles.badgeText}>{liveSessions > 9 ? "9+" : String(liveSessions)}</Text>
            </View>
          )}
        </Pressable>
      )}
      <Pressable
        onPress={onToggleStar}
        disabled={disabled}
        hitSlop={8}
        accessibilityRole="button"
        accessibilityLabel={starred ? "Unstar this conversation" : "Star this conversation"}
        accessibilityState={{ selected: starred, disabled: !!disabled }}
        style={({ pressed }) => [styles.button, pressed && styles.buttonPressed]}
      >
        <Feather name="star" size={18} color={colors.inkSoft} />
      </Pressable>
      <Pressable
        onPress={onToggleMute}
        disabled={disabled}
        hitSlop={8}
        accessibilityRole="button"
        accessibilityLabel={muted ? "Unmute this conversation" : "Mute this conversation"}
        accessibilityState={{ selected: muted, disabled: !!disabled }}
        style={({ pressed }) => [styles.button, pressed && styles.buttonPressed]}
      >
        <Feather name={muted ? "bell-off" : "bell"} size={18} color={colors.inkSoft} />
      </Pressable>
    </View>
  );
}

const styles = StyleSheet.create({
  row: { flexDirection: "row" },
  button: {
    width: 36,
    height: 36,
    borderRadius: radii.sm,
    alignItems: "center",
    justifyContent: "center",
  },
  buttonPressed: { backgroundColor: colors.rowPressed },
  badge: {
    position: "absolute",
    top: 3,
    right: 3,
    minWidth: 14,
    height: 14,
    borderRadius: 7,
    paddingHorizontal: 3,
    backgroundColor: colors.green,
    alignItems: "center",
    justifyContent: "center",
  },
  badgeText: { fontFamily: fonts.sansSemiBold, fontSize: 9, lineHeight: 11, color: colors.white },
});
