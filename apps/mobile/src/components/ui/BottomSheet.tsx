import { Host } from "@expo/ui";
import { BottomSheet as NativeSheet, Group, RNHostView } from "@expo/ui/swift-ui";
import {
  environment,
  interactiveDismissDisabled,
  presentationBackground,
  presentationDetents,
  presentationDragIndicator,
} from "@expo/ui/swift-ui/modifiers";
import { type ReactNode, useState } from "react";
import { StyleSheet, useColorScheme, useWindowDimensions, View } from "react-native";

import { spacing, useChatPalette } from "../../config/theme";

interface BottomSheetProps {
  visible: boolean;
  onClose: () => void;
  children: ReactNode;
  /**
   * Present at full height, with the content filling the sheet — for a sheet
   * with a text field and a list (the way Messages presents New Message). The
   * keyboard then shrinks the content; a content-sized sheet would instead be
   * pushed up by it until its header left the screen.
   */
  large?: boolean;
  /** Content-sized sheets: cap on height as a share of the window. A list
   * inside the card scrolls past it (the card gets `flexShrink: 1`). */
  maxHeight?: `${number}%`;
  /** `false` while the sheet must not be swiped away (e.g. mid-submit). */
  dismissible?: boolean;
}

const LOCKED = interactiveDismissDisabled(true);

/**
 * The app's bottom sheet, presented by iOS: SwiftUI's `.sheet` (@expo/ui),
 * with our React Native content hosted inside it.
 *
 * iOS owns everything the previous Reanimated sheet re-implemented — the
 * slide, the spring, drag and fling to dismiss, the dimmed backdrop, the drag
 * indicator, the home-indicator inset and the keyboard — so each sheet behaves
 * exactly like a system one, iOS 26's included.
 *
 * `onClose` fires when the reader dismisses the sheet (drag down, tap the
 * backdrop); closing it from code is `visible={false}`. Content stays mounted
 * through the native exit animation, and nothing is mounted while the sheet
 * is closed — several screens render one per list row.
 */
export function BottomSheet({
  visible,
  onClose,
  children,
  large = false,
  maxHeight = "85%",
  dismissible = true,
}: BottomSheetProps) {
  const { width, height } = useWindowDimensions();
  const dark = useColorScheme() === "dark";
  const palette = useChatPalette();
  // Holds the native host from the first open until the exit animation ends.
  const [mounted, setMounted] = useState(visible);
  if (visible && !mounted) setMounted(true);
  if (!mounted) return null;

  const shared = [
    presentationDragIndicator("visible"),
    // Follow the system scheme (was pinned light) so the sheet's own chrome —
    // and the SwiftUI-drawn scrim/drag-indicator — reads on a dark phone.
    environment({ key: "colorScheme", value: dark ? "dark" : "light" }),
    presentationBackground(palette.surface),
  ];
  const modifiers = large ? [...shared, presentationDetents(["large"])] : shared;
  return (
    <Host style={{ position: "absolute", width }} pointerEvents="none">
      <NativeSheet
        isPresented={visible}
        onIsPresentedChange={(presented) => {
          if (!presented) onClose();
        }}
        onDismiss={() => setMounted(false)}
        fitToContents={!large}
      >
        <Group modifiers={dismissible ? modifiers : [...modifiers, LOCKED]}>
          <RNHostView matchContents={!large}>
            <View
              style={
                large
                  ? styles.fill
                  : // A content-sized sheet has no height to resolve a
                    // percentage against, so the cap is in points.
                    [styles.top, { maxHeight: (parseFloat(maxHeight) / 100) * height }]
              }
            >
              {children}
            </View>
          </RNHostView>
        </Group>
      </NativeSheet>
    </Host>
  );
}

const styles = StyleSheet.create({
  // Clears the native drag indicator.
  top: { paddingTop: spacing.lg },
  // Takes the sheet's height (reported by SwiftUI, already less the keyboard)
  // rather than the list's intrinsic height.
  fill: { flexGrow: 1, height: 0, paddingTop: spacing.lg },
});
