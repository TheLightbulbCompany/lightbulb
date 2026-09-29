import { SymbolView } from "expo-symbols";
import { Fragment, useEffect, useMemo, useState, type ReactNode } from "react";
import { Pressable, ScrollView, StyleSheet, Text, TextInput, View } from "react-native";

import { fonts, radii, spacing, useChatPalette } from "../../config/theme";
import { BottomSheet } from "../ui/BottomSheet";

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
  /** Visible but not selectable, e.g. an existing conversation member. */
  disabled?: boolean;
}

export interface PickerSheetProps {
  visible: boolean;
  heading: string;
  /** Context subheading (e.g. the issue title), or null. */
  subheading?: string | null;
  options: PickerOption[];
  /** Currently-selected key (checked, non-selectable no-op), or null. */
  selectedKey?: string | null;
  /** When set, a local case-insensitive option filter is shown. */
  searchPlaceholder?: string;
  onSelect: (key: string) => void;
  onClose: () => void;
}

export function PickerSheet({
  visible,
  heading,
  subheading,
  options,
  selectedKey,
  searchPlaceholder,
  onSelect,
  onClose,
}: PickerSheetProps) {
  const p = useChatPalette();
  const [query, setQuery] = useState("");
  useEffect(() => {
    if (!visible) setQuery("");
  }, [visible]);
  const filteredOptions = useMemo(() => {
    const normalized = query.trim().toLocaleLowerCase();
    return normalized ? options.filter((option) => option.label.toLocaleLowerCase().includes(normalized)) : options;
  }, [options, query]);
  return (
    <BottomSheet
      visible={visible}
      onClose={onClose}
      maxHeight="80%"
    >
      <View style={[styles.sheet, { backgroundColor: p.surface }]}>
        <Text style={[styles.heading, { color: p.fg }]}>{heading}</Text>
        {subheading != null && subheading !== "" && (
          <Text style={[styles.subheading, { color: p.fgMute }]} numberOfLines={1}>
            {subheading}
          </Text>
        )}
        {searchPlaceholder ? (
          <TextInput
            value={query}
            onChangeText={setQuery}
            placeholder={searchPlaceholder}
            placeholderTextColor={p.fgMute}
            accessibilityLabel={searchPlaceholder}
            autoCapitalize="none"
            autoCorrect={false}
            style={[styles.search, { color: p.fg, borderColor: p.edge, backgroundColor: p.ground }]}
          />
        ) : null}
        <ScrollView
          style={styles.list}
          contentContainerStyle={styles.listContent}
          showsVerticalScrollIndicator={false}
          bounces={false}
        >
          {filteredOptions.map((option, index) => {
            const active = selectedKey === option.key;
            // Draw a group header whenever this option starts a new section.
            const prevSection = index > 0 ? filteredOptions[index - 1].section : null;
            const showHeader =
              option.section != null &&
              option.section !== "" &&
              option.section !== prevSection;
            return (
              <Fragment key={option.key}>
                {showHeader ? (
                  <Text style={[styles.sectionHeader, { color: p.fgMute }]}>
                    {option.section?.toUpperCase()}
                  </Text>
                ) : null}
                <Pressable
                  style={({ pressed }) => [
                    styles.row,
                    active && { backgroundColor: p.raised },
                    option.disabled && styles.rowDisabled,
                    pressed && !option.disabled && { backgroundColor: p.selected },
                  ]}
                  disabled={option.disabled}
                  onPress={() => {
                    if (!active && !option.disabled) onSelect(option.key);
                    if (!option.disabled) onClose();
                  }}
                  accessibilityRole="button"
                  accessibilityState={{ selected: active, disabled: option.disabled }}
                  accessibilityLabel={option.label}
                >
                  {option.dotColor ? (
                    <View style={[styles.dot, { backgroundColor: option.dotColor }]} />
                  ) : null}
                  {option.leading ?? null}
                  <Text
                    style={[
                      styles.rowLabel,
                      { color: p.fg },
                      option.strikethrough && styles.rowLabelStruck,
                    ]}
                    numberOfLines={1}
                  >
                    {option.label}
                  </Text>
                  {active ? (
                    <SymbolView accessibilityElementsHidden name="checkmark" size={16} tintColor={p.link} />
                  ) : (
                    <View style={styles.checkSpacer} />
                  )}
                </Pressable>
              </Fragment>
            );
          })}
        </ScrollView>
      </View>
    </BottomSheet>
  );
}

const styles = StyleSheet.create({
  sheet: {
    // Shrinks to BottomSheet's height cap (smaller with the keyboard up) so
    // the list inside scrolls, instead of the footer overflowing off-screen.
    flexShrink: 1,
    borderTopLeftRadius: radii.xl,
    borderTopRightRadius: radii.xl,
    paddingHorizontal: spacing.lg,
    paddingTop: spacing.sm,
    paddingBottom: spacing.md,
    gap: spacing.xs,
  },
  heading: {
    fontFamily: fonts.sansSemiBold,
    fontSize: 15,
    lineHeight: 20,
  },
  subheading: {
    fontFamily: fonts.sans,
    fontSize: 12,
    lineHeight: 16,
    marginBottom: spacing.xs,
  },
  search: {
    minHeight: 42,
    borderWidth: StyleSheet.hairlineWidth,
    borderRadius: radii.md,
    paddingHorizontal: spacing.md,
    fontFamily: fonts.sans,
    fontSize: 14,
  },
  list: { marginTop: spacing.xs },
  listContent: { gap: spacing.xs },
  sectionHeader: {
    fontFamily: fonts.sansMedium,
    fontSize: 10,
    lineHeight: 14,
    letterSpacing: 1.4,
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
  rowDisabled: { opacity: 0.55 },
  dot: { width: 12, height: 12, borderRadius: 6 },
  rowLabel: {
    flex: 1,
    fontFamily: fonts.sansMedium,
    fontSize: 14,
    lineHeight: 19,
  },
  rowLabelStruck: { textDecorationLine: "line-through", opacity: 0.7 },
  checkSpacer: { width: 16, height: 16 },
});
