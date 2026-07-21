import { type ReactNode, useEffect, useRef, useState } from "react";
import {
  Animated,
  type DimensionValue,
  Dimensions,
  Easing,
  Modal,
  Pressable,
  StyleSheet,
  View,
} from "react-native";

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

// Slide-in slower than slide-out; ease-out on entry so the card decelerates
// into place, ease-in on exit so it accelerates away.
const OPEN_MS = 240;
const CLOSE_MS = 180;

/**
 * Shared bottom-sheet chrome: a fading scrim + a card that slides up from the
 * bottom. Replaces React Native's `<Modal animationType="slide">`, which
 * translates the WHOLE modal — full-screen tint included — up from the bottom
 * (the tint appears to "slide up" instead of fading in). Here the scrim fades
 * (opacity 0→1) while ONLY the card slides (translateY), which is the native
 * bottom-sheet feel.
 *
 * The card's visual (cream fill, top radii, drag handle, padding) stays with
 * each caller's children — this component owns only the scrim + motion. On
 * close it animates out, then unmounts.
 */
export function BottomSheet({
  visible,
  onClose,
  children,
  closeLabel = "Close",
  maxHeight = "85%",
}: BottomSheetProps) {
  // `mounted` keeps the Modal alive through the close animation; it flips false
  // only once the exit finishes. The card slides its full off-screen distance —
  // the excess travel below the screen edge is invisible, so no height measure
  // is needed.
  const [mounted, setMounted] = useState(visible);
  const progress = useRef(new Animated.Value(visible ? 1 : 0)).current;

  useEffect(() => {
    if (visible) {
      setMounted(true);
      Animated.timing(progress, {
        toValue: 1,
        duration: OPEN_MS,
        easing: Easing.out(Easing.cubic),
        useNativeDriver: true,
      }).start();
    } else {
      Animated.timing(progress, {
        toValue: 0,
        duration: CLOSE_MS,
        easing: Easing.in(Easing.cubic),
        useNativeDriver: true,
      }).start(({ finished }) => {
        if (finished) setMounted(false);
      });
    }
  }, [visible, progress]);

  if (!mounted) return null;

  const translateY = progress.interpolate({
    inputRange: [0, 1],
    outputRange: [Dimensions.get("window").height, 0],
  });

  return (
    <Modal
      visible={mounted}
      transparent
      animationType="none"
      onRequestClose={onClose}
      statusBarTranslucent
    >
      <View style={styles.root}>
        <Animated.View style={[styles.scrim, { opacity: progress }]}>
          <Pressable
            style={styles.scrimFill}
            onPress={onClose}
            accessibilityLabel={closeLabel}
          />
        </Animated.View>
        <Animated.View style={{ maxHeight, transform: [{ translateY }] }}>
          {children}
        </Animated.View>
      </View>
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
