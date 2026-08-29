import { useEffect, useRef, useState } from "react";
import {
  KeyboardAvoidingView,
  Platform,
  Pressable,
  StyleSheet,
  Text,
  TextInput,
  View,
} from "react-native";

import { colors, fonts, radii, spacing } from "../../config/theme";
import { BottomSheet } from "../ui/BottomSheet";

/** Server contract for reject reasons / issue fields is ≤2000 chars. */
const MAX_LENGTH = 2000;

/**
 * Cross-platform single-text-input sheet. Two jobs:
 *  - "give a reason" (ApprovalCard / InteractionCard reject) — replaces the
 *    iOS-only `Alert.prompt` that dead-ended Android; `tone:"danger"`.
 *  - inline field edit (issue title / description) — pass `initialValue` and
 *    `tone:"primary"`.
 * `required` blocks submit on empty text.
 */
export function ReasonSheet({
  visible,
  title,
  placeholder = "Reason…",
  submitLabel = "Reject",
  required = true,
  initialValue = "",
  tone = "danger",
  onSubmit,
  onClose,
}: {
  visible: boolean;
  title: string;
  placeholder?: string;
  submitLabel?: string;
  required?: boolean;
  initialValue?: string;
  tone?: "danger" | "primary";
  onSubmit: (reason: string) => void;
  onClose: () => void;
}) {
  const [reason, setReason] = useState(initialValue);
  // Double-submit latch: onSubmit fires once per open — the sheet stays
  // mounted through its ~180ms close animation, where a second tap could land.
  const submittedRef = useRef(false);
  // Reset the draft to the (possibly new) initial value whenever the sheet
  // opens for a fresh target.
  useEffect(() => {
    if (visible) {
      setReason(initialValue);
      submittedRef.current = false;
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- initialValue is a seed, not a live binding
  }, [visible]);

  const trimmed = reason.trim();
  const canSubmit = !required || trimmed.length > 0;

  return (
    <BottomSheet visible={visible} onClose={onClose} closeLabel="Cancel">
      <KeyboardAvoidingView
        behavior={Platform.OS === "ios" ? "padding" : undefined}
      >
      <View style={styles.card}>
        <View style={styles.handle} />
        <Text style={styles.title}>{title}</Text>
        <TextInput
          style={styles.input}
          placeholder={placeholder}
          placeholderTextColor={colors.placeholder}
          value={reason}
          onChangeText={setReason}
          multiline
          autoFocus
          maxLength={MAX_LENGTH}
          accessibilityLabel={placeholder}
        />
        <View style={styles.actions}>
          <Pressable
            style={({ pressed }) => [styles.cancelBtn, pressed && styles.pressed]}
            onPress={onClose}
            accessibilityRole="button"
            accessibilityLabel="Cancel"
          >
            <Text style={styles.cancelText}>Cancel</Text>
          </Pressable>
          <Pressable
            style={({ pressed }) => [
              styles.submitBtn,
              tone === "primary" && styles.submitPrimary,
              !canSubmit && styles.submitDisabled,
              pressed && styles.pressed,
            ]}
            onPress={() => {
              if (!canSubmit || submittedRef.current) return;
              submittedRef.current = true;
              onSubmit(trimmed);
              onClose();
            }}
            disabled={!canSubmit}
            accessibilityRole="button"
            accessibilityLabel={submitLabel}
          >
            <Text style={styles.submitText}>{submitLabel}</Text>
          </Pressable>
        </View>
      </View>
      </KeyboardAvoidingView>
    </BottomSheet>
  );
}

const styles = StyleSheet.create({
  card: {
    backgroundColor: colors.cream,
    borderTopLeftRadius: radii.xl,
    borderTopRightRadius: radii.xl,
    padding: spacing.lg,
    paddingBottom: spacing.xl,
    gap: spacing.md,
  },
  handle: {
    alignSelf: "center",
    width: 36,
    height: 4,
    borderRadius: 2,
    backgroundColor: colors.creamDeepest,
  },
  title: {
    fontFamily: fonts.sansMedium,
    fontSize: 15,
    lineHeight: 20,
    color: colors.ink,
  },
  input: {
    minHeight: 72,
    maxHeight: 160,
    backgroundColor: colors.white,
    borderWidth: 1,
    borderColor: colors.edge,
    borderRadius: radii.md,
    padding: spacing.md,
    fontFamily: fonts.sans,
    fontSize: 14,
    lineHeight: 20,
    color: colors.ink,
    textAlignVertical: "top",
  },
  actions: {
    flexDirection: "row",
    justifyContent: "flex-end",
    gap: spacing.sm,
  },
  pressed: { opacity: 0.6 },
  cancelBtn: {
    paddingHorizontal: spacing.lg,
    paddingVertical: spacing.sm,
    borderRadius: radii.sm,
  },
  cancelText: {
    fontFamily: fonts.sansMedium,
    fontSize: 13,
    lineHeight: 18,
    color: colors.mute,
  },
  submitBtn: {
    paddingHorizontal: spacing.lg,
    paddingVertical: spacing.sm,
    borderRadius: radii.sm,
    backgroundColor: colors.danger,
  },
  submitPrimary: { backgroundColor: colors.forest },
  submitDisabled: { backgroundColor: colors.creamDeepest },
  submitText: {
    fontFamily: fonts.sansMedium,
    fontSize: 13,
    lineHeight: 18,
    color: colors.white,
  },
});
