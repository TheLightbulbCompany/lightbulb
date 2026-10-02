import { useEffect, type ReactNode } from "react";
import { StyleSheet, View } from "react-native";
import Animated, { useAnimatedStyle, useReducedMotion, useSharedValue, withTiming } from "react-native-reanimated";

const TICK_MS = 66; // ~15 updates/s; the timing below smooths between them

/**
 * A soft halo behind an avatar that swells with an audio level (0..1), the phone's
 * stand-in for the web orb. `level` is polled, so a caller can hand over the
 * session's `levels()` without re-rendering on every frame. Reduce Motion: no halo.
 */
export function LevelRing({ level, size, color, children }: { level: () => number; size: number; color: string; children: ReactNode }) {
  const reduced = useReducedMotion();
  const v = useSharedValue(0);
  useEffect(() => {
    if (reduced) return;
    const t = setInterval(() => { v.set(withTiming(Math.max(0, Math.min(1, level())), { duration: TICK_MS })); }, TICK_MS);
    return () => clearInterval(t);
  }, [level, reduced, v]);
  const halo = useAnimatedStyle(() => ({ opacity: 0.15 + v.get() * 0.45, transform: [{ scale: 1 + v.get() * 0.45 }] }));
  return (
    <View style={[styles.box, { width: size * 1.6, height: size * 1.6 }]}>
      {!reduced && (
        <Animated.View
          testID="level-ring"
          style={[styles.halo, { width: size * 1.1, height: size * 1.1, borderRadius: size, backgroundColor: color }, halo]}
        />
      )}
      {children}
    </View>
  );
}

const styles = StyleSheet.create({
  box: { alignItems: "center", justifyContent: "center" },
  halo: { position: "absolute" },
});
