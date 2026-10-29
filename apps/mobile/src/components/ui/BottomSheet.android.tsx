import type { ReactNode } from "react";
import { Modal, Pressable, StyleSheet, useWindowDimensions, View } from "react-native";
import { KeyboardAvoidingView } from "react-native-keyboard-controller";
import { useSafeAreaInsets } from "react-native-safe-area-context";

import { spacing, useChatPalette } from "../../config/theme";

/**
 * Android's bottom sheet: React Native's Modal sliding up over a dimmed
 * backdrop (iOS presents SwiftUI's `.sheet`; see BottomSheet.tsx). Same
 * props. Tapping the backdrop or the system back gesture closes it unless
 * `dismissible` is false.
 */
export function BottomSheet({
  visible,
  onClose,
  children,
  large = false,
  maxHeight = "85%",
  dismissible = true,
}: {
  visible: boolean;
  onClose: () => void;
  children: ReactNode;
  large?: boolean;
  maxHeight?: `${number}%`;
  dismissible?: boolean;
}) {
  const { height } = useWindowDimensions();
  const insets = useSafeAreaInsets();
  const palette = useChatPalette();
  const close = () => {
    if (dismissible) onClose();
  };
  return (
    <Modal visible={visible} transparent animationType="slide" statusBarTranslucent navigationBarTranslucent onRequestClose={close}>
      <KeyboardAvoidingView behavior="padding" style={styles.root}>
        <Pressable accessibilityLabel="Close" style={styles.backdrop} onPress={close} />
        <View
          style={[
            styles.card,
            { backgroundColor: palette.surface, paddingBottom: insets.bottom },
            large ? { height: height - insets.top - spacing.lg } : { maxHeight: (parseFloat(maxHeight) / 100) * height },
          ]}
        >
          <View style={[styles.handle, { backgroundColor: palette.edge }]} />
          <View style={large ? styles.fill : styles.shrink}>{children}</View>
        </View>
      </KeyboardAvoidingView>
    </Modal>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1, justifyContent: "flex-end" },
  backdrop: { ...StyleSheet.absoluteFill, backgroundColor: "rgba(0,0,0,0.4)" },
  card: { borderTopLeftRadius: 16, borderTopRightRadius: 16, overflow: "hidden" },
  handle: { alignSelf: "center", width: 36, height: 5, borderRadius: 3, marginTop: spacing.sm, marginBottom: spacing.sm },
  fill: { flex: 1 },
  shrink: { flexShrink: 1 },
});
