import { Pressable } from "react-native";
import Animated, { useAnimatedStyle, useSharedValue, withSpring } from "react-native-reanimated";

/** Slack/iOS icon-button feedback: a spring scale-down, not an opacity dip —
 * a filled icon button (the pencil, the jump-to-latest pill) shows no visible
 * change from a translucent press overlay, but a scale reads on any fill. */
const PRESSED_SCALE = 0.94;
const SPRING = { damping: 20, stiffness: 300 } as const;

/** A `Pressable` whose `style` prop can carry the reanimated scale style below. */
export const AnimatedPressable = Animated.createAnimatedComponent(Pressable);

/**
 * Scale-on-press for icon-only buttons. Spread `onPressIn`/`onPressOut` onto
 * an `AnimatedPressable` and include `style` in its (animated) style array.
 */
export function usePressScale(scale: number = PRESSED_SCALE) {
  const value = useSharedValue(1);
  const style = useAnimatedStyle(() => ({ transform: [{ scale: value.value }] }));
  return {
    style,
    onPressIn: () => {
      value.value = withSpring(scale, SPRING);
    },
    onPressOut: () => {
      value.value = withSpring(1, SPRING);
    },
  };
}
