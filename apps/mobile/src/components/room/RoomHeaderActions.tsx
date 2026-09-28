import { SymbolView } from "expo-symbols";
import { StyleSheet, View } from "react-native";

import { radii, useChatPalette } from "../../config/theme";
import { hapticSelection } from "../../lib/haptics";
import { AnimatedPressable, usePressScale } from "../../lib/pressScale";

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
  const palette = useChatPalette();
  const starScale = usePressScale();
  const muteScale = usePressScale();
  return (
    <View style={styles.row}>
      <AnimatedPressable
        onPress={() => {
          hapticSelection();
          onToggleStar();
        }}
        onPressIn={starScale.onPressIn}
        onPressOut={starScale.onPressOut}
        disabled={disabled}
        hitSlop={8}
        accessibilityRole="button"
        accessibilityLabel={starred ? "Unstar this conversation" : "Star this conversation"}
        accessibilityState={{ selected: starred, disabled: !!disabled }}
        // An ARRAY, never a function: reanimated only attaches an animated
        // style it can see in the prop itself, and a function style is opaque
        // to it — the scale would silently never run. The scale is the press
        // feedback; a background tint on top of it would be two signals.
        style={[styles.button, starScale.style]}
      >
        <SymbolView accessibilityElementsHidden name={starred ? "star.fill" : "star"} size={18} tintColor={palette.fgSoft} />
      </AnimatedPressable>
      <AnimatedPressable
        onPress={() => {
          hapticSelection();
          onToggleMute();
        }}
        onPressIn={muteScale.onPressIn}
        onPressOut={muteScale.onPressOut}
        disabled={disabled}
        hitSlop={8}
        accessibilityRole="button"
        accessibilityLabel={muted ? "Unmute this conversation" : "Mute this conversation"}
        accessibilityState={{ selected: muted, disabled: !!disabled }}
        style={[styles.button, muteScale.style]}
      >
        <SymbolView accessibilityElementsHidden name={muted ? "bell.slash" : "bell"} size={18} tintColor={palette.fgSoft} />
      </AnimatedPressable>
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
});
