import { useEffect, useRef, useState } from "react";
import {
  Pressable,
  StyleSheet,
  Text,
  TextInput,
  View,
} from "react-native";

import { fonts, radii, spacing, useChatPalette } from "../../config/theme";
import { BottomSheet } from "../ui/BottomSheet";

/** Server contract for reject reasons / issue fields is ≤2000 chars. */
const MAX_LENGTH = 2000;

/**
 * Cross-platform single-text-input sheet. Two jobs:
 *  - "give a reason" — replaces the
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
  const p = useChatPalette();
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
    <BottomSheet visible={visible} onClose={onClose}>
      <View style={[styles.card, { backgroundColor: p.surface }]}>
        <Text style={[styles.title, { color: p.fg }]}>{title}</Text>
        <TextInput
          style={[styles.input, { backgroundColor: p.raised, borderColor: p.edge, color: p.fg }]}
          placeholder={placeholder}
          placeholderTextColor={p.fgMute}
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
            <Text style={[styles.cancelText, { color: p.fgMute }]}>Cancel</Text>
          </Pressable>
          <Pressable
            style={({ pressed }) => [
              styles.submitBtn,
              // The one destructive solid fill this sheet allows (theme.ts's
              // state rule §06) — the reject/decline confirm, never a
              // decorative fill.
              { backgroundColor: tone === "primary" ? p.fg : p.alert },
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
            <Text style={[styles.submitText, { color: p.onFg }]}>{submitLabel}</Text>
          </Pressable>
        </View>
      </View>
    </BottomSheet>
  );
}

const styles = StyleSheet.create({
  card: {
    borderTopLeftRadius: radii.xl,
    borderTopRightRadius: radii.xl,
    padding: spacing.lg,
    paddingBottom: spacing.md,
    gap: spacing.md,
  },
  title: {
    fontFamily: fonts.sansMedium,
    fontSize: 15,
    lineHeight: 20,
  },
  input: {
    minHeight: 72,
    maxHeight: 160,
    borderWidth: 1,
    borderRadius: radii.md,
    padding: spacing.md,
    fontFamily: fonts.sans,
    fontSize: 14,
    lineHeight: 20,
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
  },
  submitBtn: {
    paddingHorizontal: spacing.lg,
    paddingVertical: spacing.sm,
    borderRadius: radii.sm,
  },
  // Fades the whole button — label, icon and busy spinner together.
  submitDisabled: { opacity: 0.5 },
  submitText: {
    fontFamily: fonts.sansMedium,
    fontSize: 13,
    lineHeight: 18,
  },
});
