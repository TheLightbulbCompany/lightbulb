import Feather from "@expo/vector-icons/Feather";
import { Fragment, type ReactNode } from "react";
import { Modal, Pressable, ScrollView, StyleSheet, Text, View } from "react-native";

import { colors, fonts, radii, spacing } from "../../config/theme";

// ---------------------------------------------------------------------------
// Generic bottom-sheet single-select picker (factored from T2a's StatusSheet).
// A slide-up cream Modal listing options with an optional colored dot; the
// selected one is checked. Tapping an option calls onSelect(key) and closes.
// Reused by StatusSheet, and the priority / assignee pickers in issue detail.
// ---------------------------------------------------------------------------

export interface PickerOption {
  key: string;
  label: string;
  /** Leading dot color (status/priority swatch); omit for plain rows (assignee). */
  dotColor?: string | null;
  /** Render the label struck-through + dimmed (e.g. `cancelled`). */
  strikethrough?: boolean;
  /**
   * Optional group label. When an option's section differs from the previously
   * rendered one, a small header row is drawn above it (e.g. "Agents"/"People"
   * in the assignee picker). Omit for flat pickers (status/priority) — no
   * headers render then, preserving the pre-v3 layout.
   */
  section?: string | null;
  /** Optional leading element (e.g. an assignee avatar) rendered before the label. */
  leading?: ReactNode;
}

export interface PickerSheetProps {
  visible: boolean;
  heading: string;
  /** Context subheading (e.g. the issue title), or null. */
  subheading?: string | null;
  options: PickerOption[];
  /** Currently-selected key (checked, non-selectable no-op), or null. */
  selectedKey?: string | null;
  onSelect: (key: string) => void;
  onClose: () => void;
}

export function PickerSheet({
  visible,
  heading,
  subheading,
  options,
  selectedKey,
  onSelect,
  onClose,
}: PickerSheetProps) {
  return (
    <Modal
      visible={visible}
      transparent
      animationType="slide"
      onRequestClose={onClose}
    >
      <Pressable style={styles.scrim} onPress={onClose} accessibilityLabel="Dismiss">
        {/* Stop propagation: taps inside the sheet must not dismiss it. */}
        <Pressable style={styles.sheet} onPress={() => {}}>
          <View style={styles.grabber} />
          <Text style={styles.heading}>{heading}</Text>
          {subheading != null && subheading !== "" && (
            <Text style={styles.subheading} numberOfLines={1}>
              {subheading}
            </Text>
          )}
          <ScrollView
            style={styles.list}
            contentContainerStyle={styles.listContent}
            showsVerticalScrollIndicator={false}
            bounces={false}
          >
            {options.map((option, index) => {
              const active = selectedKey === option.key;
              // Draw a group header whenever this option starts a new section.
              const prevSection = index > 0 ? options[index - 1].section : null;
              const showHeader =
                option.section != null &&
                option.section !== "" &&
                option.section !== prevSection;
              return (
                <Fragment key={option.key}>
                  {showHeader ? (
                    <Text style={styles.sectionHeader}>
                      {option.section?.toUpperCase()}
                    </Text>
                  ) : null}
                  <Pressable
                    style={({ pressed }) => [
                      styles.row,
                      active && styles.rowActive,
                      pressed && styles.rowPressed,
                    ]}
                    onPress={() => {
                      if (!active) onSelect(option.key);
                      onClose();
                    }}
                    accessibilityRole="button"
                    accessibilityState={{ selected: active }}
                    accessibilityLabel={option.label}
                  >
                    {option.dotColor ? (
                      <View style={[styles.dot, { backgroundColor: option.dotColor }]} />
                    ) : null}
                    {option.leading ?? null}
                    <Text
                      style={[
                        styles.rowLabel,
                        option.strikethrough && styles.rowLabelStruck,
                      ]}
                      numberOfLines={1}
                    >
                      {option.label}
                    </Text>
                    {active ? (
                      <Feather name="check" size={16} color={colors.forest} />
                    ) : (
                      <View style={styles.checkSpacer} />
                    )}
                  </Pressable>
                </Fragment>
              );
            })}
          </ScrollView>
        </Pressable>
      </Pressable>
    </Modal>
  );
}

const styles = StyleSheet.create({
  scrim: {
    flex: 1,
    backgroundColor: colors.scrim,
    justifyContent: "flex-end",
  },
  sheet: {
    backgroundColor: colors.cream,
    borderTopLeftRadius: radii.xl,
    borderTopRightRadius: radii.xl,
    paddingHorizontal: spacing.lg,
    paddingTop: spacing.sm,
    paddingBottom: spacing.xl,
    gap: spacing.xs,
    maxHeight: "80%",
  },
  grabber: {
    alignSelf: "center",
    width: 36,
    height: 4,
    borderRadius: radii.xs,
    backgroundColor: colors.edge,
    marginBottom: spacing.sm,
  },
  heading: {
    fontFamily: fonts.dmSansBold,
    fontSize: 15,
    lineHeight: 20,
    color: colors.ink,
  },
  subheading: {
    fontFamily: fonts.dmSans,
    fontSize: 12,
    lineHeight: 16,
    color: colors.mute,
    marginBottom: spacing.xs,
  },
  list: { marginTop: spacing.xs },
  listContent: { gap: spacing.xs },
  sectionHeader: {
    fontFamily: fonts.mono,
    fontSize: 10,
    lineHeight: 14,
    letterSpacing: 1.4,
    color: colors.mute,
    paddingHorizontal: spacing.sm,
    paddingTop: spacing.sm,
    paddingBottom: spacing.xs,
  },
  row: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.md,
    paddingVertical: spacing.md,
    paddingHorizontal: spacing.sm,
    borderRadius: radii.md,
  },
  rowActive: { backgroundColor: colors.white },
  rowPressed: { backgroundColor: colors.creamDeeper },
  dot: { width: 12, height: 12, borderRadius: 6 },
  rowLabel: {
    flex: 1,
    fontFamily: fonts.dmSansMedium,
    fontSize: 14,
    lineHeight: 19,
    color: colors.ink,
  },
  rowLabelStruck: { textDecorationLine: "line-through", opacity: 0.7 },
  checkSpacer: { width: 16, height: 16 },
});
