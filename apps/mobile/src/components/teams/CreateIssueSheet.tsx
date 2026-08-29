import Feather from "@expo/vector-icons/Feather";
import { useState } from "react";
import {
  ActivityIndicator,
  KeyboardAvoidingView,
  Platform,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
  View,
} from "react-native";

import { colors, fonts, radii, spacing } from "../../config/theme";
import { BottomSheet } from "../ui/BottomSheet";
import {
  BOARD_STATUSES,
  PRIORITIES,
  PRIORITY_LABELS,
  STATUS_LABELS,
  priorityColor,
  statusColors,
  type BoardStatus,
  type CreateIssueInput,
  type Priority,
} from "../../lib/teamsData";

// ---------------------------------------------------------------------------
// Create-issue / create-sub-issue bottom sheet (contract §4 CreateIssueBody).
// Title (required) + inline priority chips + (top-level only) status chips +
// optional description. Chips are inline — no nested Modals — so the keyboard
// and the sheet never fight a stacked modal. Submit is ref-latched via the
// parent's returned promise; the local `busy` state disables the button while
// the write settles, and a truthy result resets + closes the sheet.
// ---------------------------------------------------------------------------

export interface CreateIssueSheetProps {
  visible: boolean;
  /** "issue" shows the status picker (top-level); "subissue" hides it. */
  mode: "issue" | "subissue";
  /** Returns true on success (sheet resets + closes), false on failure. */
  onSubmit: (input: CreateIssueInput) => Promise<boolean>;
  onClose: () => void;
}

const DEFAULT_STATUS: BoardStatus = "todo";

function Chip({
  label,
  active,
  dotColor,
  onPress,
}: {
  label: string;
  active: boolean;
  dotColor?: string | null;
  onPress: () => void;
}) {
  return (
    <Pressable
      style={({ pressed }) => [
        styles.chip,
        active && styles.chipActive,
        pressed && styles.chipPressed,
      ]}
      onPress={onPress}
      accessibilityRole="button"
      accessibilityState={{ selected: active }}
      accessibilityLabel={label}
    >
      {dotColor ? <View style={[styles.chipDot, { backgroundColor: dotColor }]} /> : null}
      <Text style={[styles.chipText, active && styles.chipTextActive]}>{label}</Text>
    </Pressable>
  );
}

export function CreateIssueSheet({
  visible,
  mode,
  onSubmit,
  onClose,
}: CreateIssueSheetProps) {
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [status, setStatus] = useState<BoardStatus>(DEFAULT_STATUS);
  const [priority, setPriority] = useState<Priority | null>(null);
  const [busy, setBusy] = useState(false);

  const reset = () => {
    setTitle("");
    setDescription("");
    setStatus(DEFAULT_STATUS);
    setPriority(null);
    setBusy(false);
  };

  const close = () => {
    if (busy) return;
    reset();
    onClose();
  };

  const titleEmpty = title.trim().length === 0;
  const canSubmit = !titleEmpty && !busy;

  const submit = async () => {
    const trimmed = title.trim();
    if (!trimmed || busy) return;
    setBusy(true);
    const input: CreateIssueInput = { title: trimmed };
    const desc = description.trim();
    if (desc) input.description = desc;
    if (priority) input.priority = priority;
    if (mode === "issue") input.status = status;
    const ok = await onSubmit(input);
    if (ok) {
      // Close on success from BOTH paths. The board path's onSubmit closure
      // also calls onClose (idempotent double-close is harmless); the
      // sub-issue path does not, so this is the only close there.
      reset();
      onClose();
    } else {
      setBusy(false);
    }
  };

  return (
    <BottomSheet visible={visible} onClose={close} closeLabel="Dismiss" maxHeight="88%">
      <KeyboardAvoidingView
        behavior={Platform.OS === "ios" ? "padding" : undefined}
        style={styles.avoider}
      >
        <View style={styles.sheet}>
          <View style={styles.grabber} />
          <Text style={styles.heading}>
            {mode === "subissue" ? "New sub-issue" : "New issue"}
          </Text>

          <ScrollView
            style={styles.form}
            contentContainerStyle={styles.formContent}
            keyboardShouldPersistTaps="handled"
            showsVerticalScrollIndicator={false}
            bounces={false}
          >
            {/* Title (required). */}
            <View style={styles.field}>
              <Text style={styles.fieldLabel}>TITLE</Text>
              <TextInput
                style={styles.titleInput}
                placeholder="What needs doing?"
                placeholderTextColor={colors.placeholder}
                value={title}
                onChangeText={setTitle}
                autoFocus
                returnKeyType="done"
              />
            </View>

            {/* Priority chips (optional). */}
            <View style={styles.field}>
              <Text style={styles.fieldLabel}>PRIORITY</Text>
              <View style={styles.chipRow}>
                <Chip
                  label="None"
                  active={priority == null}
                  onPress={() => setPriority(null)}
                />
                {PRIORITIES.map((p) => (
                  <Chip
                    key={p}
                    label={PRIORITY_LABELS[p]}
                    active={priority === p}
                    dotColor={priorityColor(p)}
                    onPress={() => setPriority(p)}
                  />
                ))}
              </View>
            </View>

            {/* Status chips (top-level only). */}
            {mode === "issue" && (
              <View style={styles.field}>
                <Text style={styles.fieldLabel}>STATUS</Text>
                <View style={styles.chipRow}>
                  {BOARD_STATUSES.map((s) => (
                    <Chip
                      key={s}
                      label={STATUS_LABELS[s]}
                      active={status === s}
                      dotColor={statusColors(s).color}
                      onPress={() => setStatus(s)}
                    />
                  ))}
                </View>
              </View>
            )}

            {/* Description (optional). */}
            <View style={styles.field}>
              <Text style={styles.fieldLabel}>DESCRIPTION</Text>
              <TextInput
                style={styles.descInput}
                placeholder="Add more detail (optional)"
                placeholderTextColor={colors.placeholder}
                value={description}
                onChangeText={setDescription}
                multiline
                textAlignVertical="top"
              />
            </View>
          </ScrollView>

          <View style={styles.actions}>
            <Pressable
              style={({ pressed }) => [styles.cancelBtn, pressed && styles.cancelPressed]}
              onPress={close}
              disabled={busy}
              accessibilityRole="button"
              accessibilityLabel="Cancel"
            >
              <Text style={styles.cancelText}>Cancel</Text>
            </Pressable>
            <Pressable
              style={[styles.createBtn, titleEmpty && styles.createDisabled]}
              onPress={submit}
              disabled={!canSubmit}
              accessibilityRole="button"
              accessibilityLabel={mode === "subissue" ? "Create sub-issue" : "Create issue"}
            >
              {busy ? (
                <ActivityIndicator size="small" color={colors.white} />
              ) : (
                <>
                  <Feather name="plus" size={15} color={colors.white} />
                  <Text style={styles.createText}>Create</Text>
                </>
              )}
            </Pressable>
          </View>
        </View>
      </KeyboardAvoidingView>
    </BottomSheet>
  );
}

const styles = StyleSheet.create({
  avoider: { width: "100%" },
  sheet: {
    backgroundColor: colors.cream,
    borderTopLeftRadius: radii.xl,
    borderTopRightRadius: radii.xl,
    paddingHorizontal: spacing.lg,
    paddingTop: spacing.sm,
    paddingBottom: spacing.xl,
    gap: spacing.md,
  },
  grabber: {
    alignSelf: "center",
    width: 36,
    height: 4,
    borderRadius: radii.xs,
    backgroundColor: colors.edge,
    marginBottom: spacing.xs,
  },
  heading: {
    fontFamily: fonts.sansSemiBold,
    fontSize: 16,
    lineHeight: 21,
    color: colors.ink,
  },

  form: { flexGrow: 0 },
  formContent: { gap: spacing.lg, paddingBottom: spacing.sm },
  field: { gap: spacing.sm },
  fieldLabel: {
    fontFamily: fonts.mono,
    fontSize: 10,
    lineHeight: 14,
    letterSpacing: 1.4,
    color: colors.mute,
  },

  titleInput: {
    fontFamily: fonts.sansMedium,
    fontSize: 15,
    lineHeight: 20,
    color: colors.ink,
    backgroundColor: colors.creamDeeper,
    borderWidth: 1,
    borderColor: colors.edge,
    borderRadius: radii.md,
    paddingHorizontal: spacing.md,
    paddingVertical: spacing.md,
  },
  descInput: {
    fontFamily: fonts.sans,
    fontSize: 14,
    lineHeight: 20,
    color: colors.ink,
    backgroundColor: colors.creamDeeper,
    borderWidth: 1,
    borderColor: colors.edge,
    borderRadius: radii.md,
    paddingHorizontal: spacing.md,
    paddingVertical: spacing.md,
    minHeight: 72,
  },

  chipRow: { flexDirection: "row", flexWrap: "wrap", gap: spacing.sm },
  chip: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.xs,
    paddingHorizontal: spacing.md,
    paddingVertical: spacing.sm,
    borderRadius: radii.sm,
    borderWidth: 1,
    borderColor: colors.edge,
    backgroundColor: colors.white,
  },
  chipActive: { borderColor: colors.forest, backgroundColor: colors.forestTint },
  chipPressed: { opacity: 0.6 },
  chipDot: { width: 9, height: 9, borderRadius: 5 },
  chipText: {
    fontFamily: fonts.sansMedium,
    fontSize: 12,
    lineHeight: 16,
    color: colors.mute,
  },
  chipTextActive: { color: colors.forest },

  actions: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "flex-end",
    gap: spacing.sm,
  },
  cancelBtn: {
    paddingHorizontal: spacing.lg,
    paddingVertical: spacing.md,
    borderRadius: radii.sm,
  },
  cancelPressed: { opacity: 0.6 },
  cancelText: {
    fontFamily: fonts.sansMedium,
    fontSize: 14,
    lineHeight: 18,
    color: colors.mute,
  },
  createBtn: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "center",
    gap: spacing.xs,
    minWidth: 108,
    paddingHorizontal: spacing.lg,
    paddingVertical: spacing.md,
    borderRadius: radii.sm,
    backgroundColor: colors.forest,
  },
  createDisabled: { opacity: 0.5 },
  createText: {
    fontFamily: fonts.sansSemiBold,
    fontSize: 14,
    lineHeight: 18,
    color: colors.white,
  },
});
