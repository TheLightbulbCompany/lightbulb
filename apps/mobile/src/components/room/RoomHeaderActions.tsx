import Feather from "@expo/vector-icons/Feather";
import { Pressable, StyleSheet, View } from "react-native";

import { colors, radii } from "../../config/theme";

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
}: {
  starred: boolean;
  muted: boolean;
  disabled?: boolean;
  onToggleStar: () => void;
  onToggleMute: () => void;
}) {
  return (
    <View style={styles.row}>
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
});
