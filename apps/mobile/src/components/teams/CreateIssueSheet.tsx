import { SymbolView } from "../ui/SymbolView";
import { useState } from "react";
import {
  ActivityIndicator,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
  View,
} from "react-native";

import { fonts, radii, spacing, useChatPalette, type ChatPalette } from "../../config/theme";
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
// work-mode chips + optional description. Chips are inline — no nested Modals — so the keyboard
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

// How the assignee runs the task. Values and order follow the web composer
// (upstream Paperclip's work-mode meta: Auto / Plan / Ask).
type WorkMode = NonNullable<CreateIssueInput["workMode"]>;
const WORK_MODES: { value: WorkMode; label: string }[] = [
  { value: "standard", label: "Auto" },
  { value: "planning", label: "Plan" },
  { value: "ask", label: "Ask" },
];

function Chip({
  label,
  active,
  dotColor,
  onPress,
  p,
}: {
  label: string;
  active: boolean;
  dotColor?: string | null;
  onPress: () => void;
  p: ChatPalette;
}) {
  return (
    <Pressable
      style={({ pressed }) => [
        styles.chip,
        { borderColor: p.edge, backgroundColor: p.surface },
        active && { borderColor: p.fg, backgroundColor: p.chip },
        pressed && styles.chipPressed,
      ]}
      onPress={onPress}
      accessibilityRole="button"
      accessibilityState={{ selected: active }}
      accessibilityLabel={label}
    >
      {dotColor ? <View style={[styles.chipDot, { backgroundColor: dotColor }]} /> : null}
      <Text style={[styles.chipText, { color: p.fgMute }, active && { color: p.fg }]}>{label}</Text>
    </Pressable>
  );
}

export function CreateIssueSheet({
  visible,
  mode,
  onSubmit,
  onClose,
}: CreateIssueSheetProps) {
  const p = useChatPalette();
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [status, setStatus] = useState<BoardStatus>(DEFAULT_STATUS);
  const [priority, setPriority] = useState<Priority | null>(null);
  const [workMode, setWorkMode] = useState<WorkMode>("standard");
  const [busy, setBusy] = useState(false);

  const reset = () => {
    setTitle("");
    setDescription("");
    setStatus(DEFAULT_STATUS);
    setPriority(null);
    setWorkMode("standard");
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
    input.workMode = workMode;
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
    <BottomSheet visible={visible} onClose={close} large dismissible={!busy}>
      <View style={[styles.sheet, { backgroundColor: p.surface }]}>
        <Text style={[styles.heading, { color: p.fg }]}>
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
            <Text style={[styles.fieldLabel, { color: p.fgMute }]}>TITLE</Text>
            <TextInput
              style={[styles.titleInput, { color: p.fg, backgroundColor: p.raised, borderColor: p.edge }]}
              placeholder="What needs doing?"
              placeholderTextColor={p.fgMute}
              value={title}
              onChangeText={setTitle}
              autoFocus
              returnKeyType="done"
            />
          </View>

          {/* Priority chips (optional). */}
          <View style={styles.field}>
            <Text style={[styles.fieldLabel, { color: p.fgMute }]}>PRIORITY</Text>
            <View style={styles.chipRow}>
              <Chip
                label="None"
                active={priority == null}
                onPress={() => setPriority(null)}
                p={p}
              />
              {PRIORITIES.map((option) => (
                <Chip
                  key={option}
                  label={PRIORITY_LABELS[option]}
                  active={priority === option}
                  dotColor={priorityColor(option)}
                  onPress={() => setPriority(option)}
                  p={p}
                />
              ))}
            </View>
          </View>

          {/* Status chips (top-level only). */}
          {mode === "issue" && (
            <View style={styles.field}>
              <Text style={[styles.fieldLabel, { color: p.fgMute }]}>STATUS</Text>
              <View style={styles.chipRow}>
                {BOARD_STATUSES.map((s) => (
                  <Chip
                    key={s}
                    label={STATUS_LABELS[s]}
                    active={status === s}
                    dotColor={statusColors(s).color}
                    onPress={() => setStatus(s)}
                    p={p}
                  />
                ))}
              </View>
            </View>
          )}

          {/* Work mode chips: Auto does the work, Plan writes a plan to accept
              first, Ask answers without doing the work. */}
          <View style={styles.field}>
            <Text style={[styles.fieldLabel, { color: p.fgMute }]}>WORK MODE</Text>
            <View style={styles.chipRow}>
              {WORK_MODES.map((option) => (
                <Chip
                  key={option.value}
                  label={option.label}
                  active={workMode === option.value}
                  onPress={() => setWorkMode(option.value)}
                  p={p}
                />
              ))}
            </View>
          </View>

          {/* Description (optional). */}
          <View style={styles.field}>
            <Text style={[styles.fieldLabel, { color: p.fgMute }]}>DESCRIPTION</Text>
            <TextInput
              style={[styles.descInput, { color: p.fg, backgroundColor: p.raised, borderColor: p.edge }]}
              placeholder="Add more detail (optional)"
              placeholderTextColor={p.fgMute}
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
            <Text style={[styles.cancelText, { color: p.fgMute }]}>Cancel</Text>
          </Pressable>
          <Pressable
            style={[styles.createBtn, { backgroundColor: p.fg }, titleEmpty && styles.createDisabled]}
            onPress={submit}
            disabled={!canSubmit}
            accessibilityRole="button"
            accessibilityLabel={mode === "subissue" ? "Create sub-issue" : "Create issue"}
          >
            {busy ? (
              <ActivityIndicator size="small" color={p.onFg} />
            ) : (
              <>
                <SymbolView accessibilityElementsHidden name="plus" size={15} tintColor={p.onFg} />
                <Text style={[styles.createText, { color: p.onFg }]}>Create</Text>
              </>
            )}
          </Pressable>
        </View>
      </View>
    </BottomSheet>
  );
}

const styles = StyleSheet.create({
  sheet: {
    // Fills the full-height sheet: the list takes the room and the footer
    // stays at the bottom — above the keyboard while typing.
    flex: 1,
    borderTopLeftRadius: radii.xl,
    borderTopRightRadius: radii.xl,
    paddingHorizontal: spacing.lg,
    paddingTop: spacing.sm,
    paddingBottom: spacing.md,
    gap: spacing.md,
  },
  heading: {
    fontFamily: fonts.sansSemiBold,
    fontSize: 16,
    lineHeight: 21,
  },

  // Takes the full-height sheet's spare room, so the footer sits at the bottom.
  form: { flexGrow: 1 },
  formContent: { gap: spacing.lg, paddingBottom: spacing.sm },
  field: { gap: spacing.sm },
  fieldLabel: {
    fontFamily: fonts.mono,
    fontSize: 10,
    lineHeight: 14,
    letterSpacing: 1.4,
  },

  titleInput: {
    fontFamily: fonts.sansMedium,
    fontSize: 15,
    lineHeight: 20,
    borderWidth: 1,
    borderRadius: radii.md,
    paddingHorizontal: spacing.md,
    paddingVertical: spacing.md,
  },
  descInput: {
    fontFamily: fonts.sans,
    fontSize: 14,
    lineHeight: 20,
    borderWidth: 1,
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
  },
  chipPressed: { opacity: 0.6 },
  chipDot: { width: 9, height: 9, borderRadius: 5 },
  chipText: {
    fontFamily: fonts.sansMedium,
    fontSize: 12,
    lineHeight: 16,
  },

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
  },
  createDisabled: { opacity: 0.5 },
  createText: {
    fontFamily: fonts.sansSemiBold,
    fontSize: 14,
    lineHeight: 18,
  },
});
