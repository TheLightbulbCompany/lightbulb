import type { LocalSession } from "@lightbulb/shared";
import { StyleSheet, View } from "react-native";

import { colors } from "../../config/theme";

const DOT: Record<LocalSession["status"], { color: string; hollow: boolean }> = {
  busy: { color: colors.green, hollow: false },
  "waiting-approval": { color: colors.warn, hollow: false },
  idle: { color: colors.mute, hollow: false },
  unknown: { color: colors.mute, hollow: false },
  offline: { color: colors.border, hollow: true },
};

/** Never colour alone: every call site already narrates the status in adjacent text. */
export function SessionStatusDot({ status }: { status: LocalSession["status"] }) {
  const dot = DOT[status];
  return (
    <View
      style={[styles.dot, dot.hollow ? { borderWidth: 1.5, borderColor: dot.color } : { backgroundColor: dot.color }]}
    />
  );
}

const styles = StyleSheet.create({
  dot: { width: 8, height: 8, borderRadius: 4 },
});
