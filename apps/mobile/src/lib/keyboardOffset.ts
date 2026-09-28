import { Platform } from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";

// iOS 26 drew the navigation bar 10pt taller. On the old 44 the composer sat
// that far inside the keyboard, which cut the bottom off the field and the
// send button.
const HEADER_HEIGHT = Platform.OS !== "ios" ? 56 : parseInt(String(Platform.Version), 10) >= 26 ? 54 : 44;

/** Air between the composer and the keyboard, on top of the composer's own padding. */
const KEYBOARD_GAP = 8;

/**
 * Native-stack content starts below its compact header. Keep the offset
 * deterministic instead of relying on KeyboardAvoidingView's mount-time
 * window measurement: that measurement can run before a pushed screen enters
 * the active window and then retain parent-relative coordinates for the life
 * of the screen.
 *
 * The negative bottom inset pairs with the composer's own safe-area padding,
 * placing its outer box inside the keyboard by exactly that inset; the gap
 * then lifts the field clear of the keyboard instead of resting on it.
 */
export function usePushedScreenKeyboardOffset(): number {
  const insets = useSafeAreaInsets();
  return insets.top + HEADER_HEIGHT - insets.bottom + KEYBOARD_GAP;
}
