import { useEffect, useRef } from "react";
import { AccessibilityInfo, Animated } from "react-native";

/**
 * Gentle opacity loop for "activity" affordances (thinking glyph, skeleton
 * shimmer). Sits at 1 (static) when disabled. When the user prefers reduced
 * motion, an active pulse holds a static dimmed opacity instead of animating
 * so "thinking" stays visually distinct from idle. `delayMs` staggers the
 * loop start (§7 skeleton rows).
 */

// Static opacity for active-but-not-animating (reduce motion): dimmed enough
// to read as "busy", bright enough to stay legible.
const REDUCE_MOTION_ACTIVE_OPACITY = 0.55;
export function usePulse(
  enabled: boolean,
  dim: number,
  durationMs = 700,
  delayMs = 0,
) {
  const value = useRef(new Animated.Value(1)).current;
  useEffect(() => {
    if (!enabled) {
      value.setValue(1);
      return;
    }
    let cancelled = false;
    let anim: Animated.CompositeAnimation | undefined;
    AccessibilityInfo.isReduceMotionEnabled()
      .then((reduceMotion) => {
        if (cancelled) return;
        if (reduceMotion) {
          value.setValue(REDUCE_MOTION_ACTIVE_OPACITY);
          return;
        }
        anim = Animated.sequence([
          Animated.delay(delayMs),
          Animated.loop(
            Animated.sequence([
              Animated.timing(value, {
                toValue: dim,
                duration: durationMs,
                useNativeDriver: true,
              }),
              Animated.timing(value, {
                toValue: 1,
                duration: durationMs,
                useNativeDriver: true,
              }),
            ]),
          ),
        ]);
        anim.start();
      })
      .catch(() => {
        // Best-effort: no reduced-motion signal means we simply don't pulse.
      });
    return () => {
      cancelled = true;
      anim?.stop();
      value.setValue(1);
    };
  }, [enabled, dim, durationMs, delayMs, value]);
  return value;
}
