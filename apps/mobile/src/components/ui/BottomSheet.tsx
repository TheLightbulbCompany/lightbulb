import { type ReactNode, useCallback, useEffect, useMemo, useState } from "react";
import { type DimensionValue, Dimensions, Modal, Pressable, StyleSheet } from "react-native";
import { Gesture, GestureDetector, GestureHandlerRootView } from "react-native-gesture-handler";
import Animated, {
  Extrapolation,
  interpolate,
  runOnJS,
  useAnimatedStyle,
  useSharedValue,
  withSpring,
} from "react-native-reanimated";

import { colors } from "../../config/theme";

interface BottomSheetProps {
  visible: boolean;
  onClose: () => void;
  children: ReactNode;
  /** Accessibility label for the scrim (the tap-to-dismiss target). */
  closeLabel?: string;
  /**
   * Cap on the card's height, resolved against the full-screen root (e.g.
   * "70%"). Applied to the sliding wrapper so callers can drop `maxHeight`
   * from their own card style (a percentage there would have no definite
   * containing height to resolve against).
   */
  maxHeight?: DimensionValue;
}

/**
 * Apple's sheet spring — damping ratio ~0.8, response ~0.3s — translated into
 * reanimated's `withSpring` stiffness/damping/mass terms via the standard
 * SwiftUI `.spring(response:dampingFraction:)` conversion (mass = 1):
 *   ω (angular frequency) = 2π / response
 *   stiffness = ω² · mass
 *   damping   = 2 · dampingRatio · ω · mass
 * response=0.3s, dampingRatio=0.8 → stiffness ≈ 439, damping ≈ 33.5.
 */
const RESPONSE_SECONDS = 0.3;
const DAMPING_RATIO = 0.8;
const MASS = 1;
const ANGULAR_FREQUENCY = (2 * Math.PI) / RESPONSE_SECONDS;
const SPRING_CONFIG = {
  mass: MASS,
  stiffness: ANGULAR_FREQUENCY ** 2 * MASS,
  damping: 2 * DAMPING_RATIO * ANGULAR_FREQUENCY * MASS,
  overshootClamping: false,
};

// How far the card travels once fully closed — more than enough to clear any
// card height regardless of content, the same trick the old Animated.timing
// version used for its off-screen translateY.
const OFFSCREEN_Y = Dimensions.get("window").height;

// Release-velocity dismiss decision: projects the finger's momentum forward
// (current position + velocity · a short time constant) rather than reading
// raw drag distance, so a fast flick dismisses even from a small drag and a
// slow drag under the distance threshold snaps back — velocity SIGN drives
// the decision, position is only the fallback for a near-zero-velocity release.
const PROJECTION_SECONDS = 0.2;
const DISMISS_DISTANCE = 100;
// Dragging up past the open position resists instead of moving 1:1.
const RUBBER_BAND_FACTOR = 0.55;

/**
 * Shared bottom-sheet chrome: a fading scrim + a card that springs up from the
 * bottom, drag-to-dismiss on the whole card (not just a handle), and
 * interruptible mid-animation — grabbing the card takes over from wherever it
 * currently is, at its current velocity, because both the gesture and the
 * open/close animation drive the SAME reanimated shared value.
 *
 * `onClose` fires once — from whichever path finishes the close (a
 * WithSpring animation that reaches `OFFSCREEN_Y`), never synchronously from
 * a tap or drag — so a caller that keeps this component mounted and only
 * toggles `visible` always sees the exit animation play before its state
 * updates. The scrim's own tap handler goes through the same close path, so
 * scrim, drag and a programmatic `visible=false` all animate identically.
 */
export function BottomSheet({
  visible,
  onClose,
  children,
  closeLabel = "Close",
  maxHeight = "85%",
}: BottomSheetProps) {
  // Keeps the Modal (and the gesture) alive through the close animation;
  // flips false only once the close spring's `finished` callback runs.
  const [mounted, setMounted] = useState(visible);
  const translateY = useSharedValue(OFFSCREEN_Y);
  const dragStartY = useSharedValue(0);

  const finishClose = useCallback(() => {
    setMounted(false);
    onClose();
  }, [onClose]);

  const closeSheet = useCallback(() => {
    translateY.value = withSpring(OFFSCREEN_Y, SPRING_CONFIG, (finished) => {
      if (finished) runOnJS(finishClose)();
    });
  }, [finishClose, translateY]);

  // `translateY` always STARTS at OFFSCREEN_Y (its useSharedValue initializer,
  // above), regardless of the initial `visible` value, so a sheet that mounts
  // already-open still springs in on this same effect rather than appearing
  // instantly — matching the old Animated.timing version's "progress always
  // starts at 0" comment.
  useEffect(() => {
    if (visible) {
      setMounted(true);
      translateY.value = withSpring(0, SPRING_CONFIG);
    } else if (mounted) {
      // Only animate closed (and eventually call onClose) if the sheet was
      // actually shown before — a sheet that mounts already-hidden (the
      // common case: no long-press yet) has nothing to close.
      closeSheet();
    }
    // closeSheet/translateY are stable across the props that matter here;
    // only a `visible` flip should re-trigger this. `mounted` is read, not a
    // dep — it's this render's latest value, and including it would refire
    // the effect on the setMounted(false) this same close eventually causes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [visible]);

  const pan = useMemo(
    () =>
      Gesture.Pan()
        .withTestId("bottom-sheet-pan")
        .onStart(() => {
          dragStartY.value = translateY.value;
        })
        .onUpdate((event) => {
          const next = dragStartY.value + event.translationY;
          translateY.value = next < 0 ? next * RUBBER_BAND_FACTOR : next;
        })
        .onEnd((event) => {
          const projected = translateY.value + event.velocityY * PROJECTION_SECONDS;
          if (projected > DISMISS_DISTANCE) {
            translateY.value = withSpring(OFFSCREEN_Y, SPRING_CONFIG, (finished) => {
              if (finished) runOnJS(finishClose)();
            });
          } else {
            translateY.value = withSpring(0, SPRING_CONFIG);
          }
        }),
    [dragStartY, finishClose, translateY],
  );

  const cardStyle = useAnimatedStyle(() => ({
    transform: [{ translateY: translateY.value }],
  }));
  const scrimStyle = useAnimatedStyle(() => ({
    opacity: interpolate(translateY.value, [0, OFFSCREEN_Y], [1, 0], Extrapolation.CLAMP),
  }));

  if (!mounted) return null;

  return (
    <Modal
      visible={mounted}
      transparent
      animationType="none"
      onRequestClose={closeSheet}
      statusBarTranslucent
    >
      {/* gesture-handler needs its own root per native window — a Modal on iOS
          presents in a separate UIWindow, so the app root's GestureHandlerRootView
          does not cover it. That same native modal presentation is also what
          clears a NativeTabs screen's UITabBarController: a Modal is a distinct
          view controller stacked above the whole window, not a sibling view in
          the RN tree, so it sits above the tab bar regardless of which tab
          presented it. */}
      <GestureHandlerRootView style={styles.root}>
        <Animated.View style={[styles.scrim, scrimStyle]}>
          <Pressable
            style={styles.scrimFill}
            onPress={closeSheet}
            accessibilityLabel={closeLabel}
          />
        </Animated.View>
        <GestureDetector gesture={pan}>
          <Animated.View style={[{ maxHeight }, cardStyle]}>{children}</Animated.View>
        </GestureDetector>
      </GestureHandlerRootView>
    </Modal>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1 },
  // Fills the space above the card (flex:1) and carries the tint; tapping it
  // dismisses. The card is a sibling below it, so it sits on top in its bounds.
  scrim: { flex: 1, backgroundColor: colors.scrim },
  scrimFill: { flex: 1 },
});
