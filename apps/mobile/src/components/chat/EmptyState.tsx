import { StyleSheet, Text, View } from "react-native";

import { colors, fonts, spacing } from "../../config/theme";

/**
 * Empty-chat hero (spec §7): centered "8" glyph, agent name in Lora, and a
 * one-line prompt. Rendered while the transcript has no items.
 */
export function EmptyState({ agentName }: { agentName: string }) {
  return (
    <View style={styles.wrap}>
      <Text style={styles.glyph}>8</Text>
      <Text style={styles.name} numberOfLines={2}>
        {agentName}
      </Text>
      <Text style={styles.sub}>Start a conversation with your agent</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  wrap: {
    flex: 1,
    alignItems: "center",
    justifyContent: "center",
    gap: spacing.sm,
    padding: spacing.xl,
  },
  glyph: {
    fontFamily: fonts.loraItalic,
    fontSize: 56,
    lineHeight: 66,
    color: colors.ink,
  },
  name: {
    fontFamily: fonts.lora,
    fontSize: 30,
    lineHeight: 38,
    letterSpacing: -0.75,
    color: colors.ink,
    textAlign: "center",
  },
  sub: {
    fontFamily: fonts.dmSans,
    fontSize: 16,
    lineHeight: 24,
    color: colors.mute,
    textAlign: "center",
  },
});
