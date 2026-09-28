import { Animated, StyleSheet, View, type StyleProp, type ViewStyle } from "react-native";

import { radii, spacing, useChatPalette } from "../../config/theme";
import { usePulse } from "./usePulse";

/**
 * Loading-history skeleton (spec §7): a user-bubble ghost followed by
 * assistant text-line ghosts pulsing at staggered 150–750ms delays.
 * Reduce-motion collapses the pulse to static fills (via usePulse).
 *
 * Fills come from the chat palette, one step off the ground in either scheme:
 * the static light fill drew bright white bars on the dark timeline.
 */
function SkeletonBlock({
  delayMs,
  style,
}: {
  delayMs: number;
  style: StyleProp<ViewStyle>;
}) {
  const pulse = usePulse(true, 0.55, 700, delayMs);
  return <Animated.View style={[style, { opacity: pulse }]} />;
}

export function TimelineSkeleton() {
  const fill = { backgroundColor: useChatPalette().raised };
  return (
    <View style={styles.wrap}>
      <SkeletonBlock delayMs={150} style={[styles.userGhost, fill]} />
      <View style={styles.assistantGroup}>
        <SkeletonBlock delayMs={300} style={[styles.lineWide, fill]} />
        <SkeletonBlock delayMs={450} style={[styles.lineMid, fill]} />
        <SkeletonBlock delayMs={600} style={[styles.lineNarrow, fill]} />
      </View>
    </View>
  );
}

const line: ViewStyle = {
  height: 14,
  borderRadius: radii.xs,
};

const styles = StyleSheet.create({
  wrap: { gap: spacing.xl },
  userGhost: {
    alignSelf: "flex-end",
    height: 36,
    width: "40%",
    borderRadius: radii.xl,
    borderBottomRightRadius: radii.sm,
  },
  assistantGroup: { gap: spacing.sm },
  lineWide: { ...line, width: "90%" },
  lineMid: { ...line, width: "72%" },
  lineNarrow: { ...line, width: "55%" },
});
