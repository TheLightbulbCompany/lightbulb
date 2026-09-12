import Feather from "@expo/vector-icons/Feather";
import { router } from "expo-router";
import { Pressable, StyleSheet } from "react-native";

import { colors, spacing } from "../../config/theme";

/** Whatever `router.replace` accepts — typed routes make that a union, not a string. */
type Fallback = Parameters<typeof router.replace>[0];

/**
 * The room stack's back affordance.
 *
 * `room/_layout.tsx` is its own Stack and `room/[roomId]/index.tsx` is that
 * Stack's ROOT, so the native header draws no back button on it — a room
 * opened from Home, DMs or a push notification was a dead end. A thread has
 * the same shape when a push deep-links straight into it.
 *
 * `router.back()` covers every case where there is history: a thread pushed
 * over its room pops to the room, and a room pushed from a tab returns to the
 * tab you came FROM (the Tabs navigator carries `backBehavior="history"`, so
 * it is not always Home). `fallback` is only for a cold start whose very first
 * screen is this one.
 */
export function HeaderBack({ fallback }: { fallback: Fallback }) {
  return (
    <Pressable
      onPress={() => (router.canGoBack() ? router.back() : router.replace(fallback))}
      accessibilityRole="button"
      accessibilityLabel="Back"
      hitSlop={spacing.sm}
      style={({ pressed }) => [styles.button, pressed && styles.pressed]}
    >
      <Feather name="chevron-left" size={26} color={colors.forest} />
    </Pressable>
  );
}

const styles = StyleSheet.create({
  // iOS puts the back chevron hard against the leading edge; the width is the
  // touch floor, not the glyph.
  button: { width: 44, height: 44, justifyContent: "center", marginLeft: -spacing.sm },
  pressed: { opacity: 0.5 },
});
