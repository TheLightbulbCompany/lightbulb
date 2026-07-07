import { Animated, StyleSheet, View, type ViewStyle } from "react-native";

import { colors, radii, spacing } from "../../config/theme";
import { usePulse } from "./usePulse";

/**
 * Loading-history skeleton (spec §7): a user-bubble ghost followed by
 * assistant text-line ghosts pulsing at staggered 150–750ms delays.
 * Reduce-motion collapses the pulse to static fills (via usePulse).
 *
 * Not yet mounted — the transcript is in-memory only until session history
 * lands (plan Phase 2 Task 5); this is the ready-made loading state for it.
 */
function SkeletonBlock({
  delayMs,
  style,
}: {
  delayMs: number;
  style: ViewStyle;
}) {
  const pulse = usePulse(true, 0.55, 700, delayMs);
  return <Animated.View style={[style, { opacity: pulse }]} />;
}

export function TimelineSkeleton() {
  return (
    <View style={styles.wrap}>
      <SkeletonBlock delayMs={150} style={styles.userGhost} />
      <View style={styles.assistantGroup}>
        <SkeletonBlock delayMs={300} style={styles.lineWide} />
        <SkeletonBlock delayMs={450} style={styles.lineMid} />
        <SkeletonBlock delayMs={600} style={styles.lineNarrow} />
      </View>
    </View>
  );
}

const line: ViewStyle = {
  height: 14,
  borderRadius: radii.xs,
  backgroundColor: colors.skeleton,
};

const styles = StyleSheet.create({
  wrap: { gap: spacing.xl },
  userGhost: {
    alignSelf: "flex-end",
    height: 36,
    width: "40%",
    borderRadius: radii.xl,
    borderBottomRightRadius: radii.sm,
    backgroundColor: colors.userBubble,
  },
  assistantGroup: { gap: spacing.sm },
  lineWide: { ...line, width: "90%" },
  lineMid: { ...line, width: "72%" },
  lineNarrow: { ...line, width: "55%" },
});
