import { SymbolView } from "expo-symbols";
import { StyleSheet, Text, View } from "react-native";

import { colors, fonts, radii } from "../../config/theme";
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
  const devicesScale = usePressScale();
  const starScale = usePressScale();
  const muteScale = usePressScale();
  return (
    <View style={styles.row}>
      {onOpenDevices && (
        <AnimatedPressable
          onPress={() => {
            hapticSelection();
            onOpenDevices();
          }}
          onPressIn={devicesScale.onPressIn}
          onPressOut={devicesScale.onPressOut}
          disabled={disabled}
          hitSlop={8}
          accessibilityRole="button"
          accessibilityLabel={liveSessions > 0 ? `Devices & sessions, ${liveSessions} live` : "Devices & sessions"}
          accessibilityState={{ disabled: !!disabled }}
          style={[styles.button, devicesScale.style]}
        >
          <SymbolView name="terminal" size={18} tintColor={colors.inkSoft} />
          {liveSessions > 0 && (
            <View style={styles.badge}>
              <Text style={styles.badgeText}>{liveSessions > 9 ? "9+" : String(liveSessions)}</Text>
            </View>
          )}
        </AnimatedPressable>
      )}
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
        <SymbolView name={starred ? "star.fill" : "star"} size={18} tintColor={colors.inkSoft} />
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
        <SymbolView name={muted ? "bell.slash" : "bell"} size={18} tintColor={colors.inkSoft} />
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
