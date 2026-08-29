import { StyleSheet, Text, View } from "react-native";

import { colors, fonts, radii } from "../../config/theme";

/**
 * 28px tinted avatar tile — Lora italic-bold initial, or the agent's emoji
 * inside the same tile when one is set (spec §5 Monogram). Mirrors the
 * private Monogram in chat.tsx; extracted so the Agents panel + inspector
 * reuse the identical roster styling without touching the chat source.
 */
export function AgentMonogram({
  name,
  emoji,
  size = 28,
}: {
  name: string;
  emoji?: string;
  size?: number;
}) {
  return (
    <View style={[styles.monogram, { width: size, height: size }]}>
      {emoji ? (
        <Text style={{ fontSize: Math.round(size * 0.5) }}>{emoji}</Text>
      ) : (
        <Text
          style={[styles.monogramLetter, { fontSize: Math.round(size * 0.5) }]}
        >
          {(name.trim()[0] ?? "L").toUpperCase()}
        </Text>
      )}
    </View>
  );
}

/** Static "active" status dot with soft halo (spec §5 StatusDot). */
export function AgentStatusDot() {
  return (
    <View style={styles.statusHalo}>
      <View style={styles.statusDot} />
    </View>
  );
}

const styles = StyleSheet.create({
  monogram: {
    borderRadius: radii.md,
    backgroundColor: colors.forestTint,
    alignItems: "center",
    justifyContent: "center",
  },
  monogramLetter: {
    fontFamily: fonts.loraBoldItalic,
    color: colors.forest,
  },
  statusHalo: {
    width: 13,
    height: 13,
    borderRadius: 7,
    backgroundColor: colors.forestHalo,
    alignItems: "center",
    justifyContent: "center",
  },
  statusDot: {
    width: 7,
    height: 7,
    borderRadius: 4,
    backgroundColor: colors.forestSoft,
  },
});
