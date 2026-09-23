import { Platform } from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";

/**
 * Native-stack content starts below its compact header. Keep the offset
 * deterministic instead of relying on KeyboardAvoidingView's mount-time
 * window measurement: that measurement can run before a pushed screen enters
 * the active window and then retain parent-relative coordinates for the life
 * of the screen.
 *
 * The negative bottom inset pairs with the composer's own safe-area padding,
 * placing its outer box inside the keyboard by exactly that inset while the
 * visible controls remain flush above it.
 */
export function usePushedScreenKeyboardOffset(): number {
  const insets = useSafeAreaInsets();
  const headerHeight = Platform.OS === "ios" ? 44 : 56;
  return insets.top + headerHeight - insets.bottom;
}
