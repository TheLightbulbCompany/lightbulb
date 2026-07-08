import Feather from "@expo/vector-icons/Feather";
import { Modal, Pressable, StyleSheet, Text, View } from "react-native";

import { colors, fonts, radii, spacing } from "../../config/theme";
import {
  BOARD_STATUSES,
  STATUS_LABELS,
  statusColors,
  type BoardStatus,
} from "../../lib/teamsData";

// ---------------------------------------------------------------------------
// Status picker bottom-sheet (contract §6 — the touch-friendly stand-in for
// drag). Slide-up cream Modal listing the 7 board statuses with colored dots;
// the current one is checked. Tapping a status calls onSelect and closes.
// ---------------------------------------------------------------------------

export interface StatusSheetProps {
  visible: boolean;
  /** Title of the issue being moved (context header), or null. */
  issueTitle: string | null;
  /** The issue's current status (checked + non-selectable no-op). */
  currentStatus: string | null | undefined;
  onSelect: (status: BoardStatus) => void;
  onClose: () => void;
}

export function StatusSheet({
  visible,
  issueTitle,
  currentStatus,
  onSelect,
  onClose,
}: StatusSheetProps) {
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
          <Text style={styles.heading}>Move to status</Text>
          {issueTitle != null && (
            <Text style={styles.subheading} numberOfLines={1}>
              {issueTitle}
            </Text>
          )}
          <View style={styles.list}>
            {BOARD_STATUSES.map((status: BoardStatus) => {
              const { color } = statusColors(status);
              const active = currentStatus === status;
              return (
                <Pressable
                  key={status}
                  style={({ pressed }) => [
                    styles.row,
                    active && styles.rowActive,
                    pressed && styles.rowPressed,
                  ]}
                  onPress={() => {
                    if (!active) onSelect(status);
                    onClose();
                  }}
                  accessibilityRole="button"
                  accessibilityState={{ selected: active }}
                  accessibilityLabel={STATUS_LABELS[status]}
                >
                  <View style={[styles.dot, { backgroundColor: color }]} />
                  <Text style={styles.rowLabel}>{STATUS_LABELS[status]}</Text>
                  {active ? (
                    <Feather name="check" size={16} color={colors.forest} />
                  ) : (
                    <View style={styles.checkSpacer} />
                  )}
                </Pressable>
              );
            })}
          </View>
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
  checkSpacer: { width: 16, height: 16 },
});
