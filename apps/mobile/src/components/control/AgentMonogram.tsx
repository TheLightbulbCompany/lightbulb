import { StyleSheet, Text, View } from "react-native";

import { fonts, radii, useChatPalette } from "../../config/theme";

/**
 * 28px tinted avatar tile — Satoshi bold-italic initial, or the agent's emoji
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
  const palette = useChatPalette();
  return (
    <View style={[styles.monogram, { width: size, height: size, backgroundColor: palette.agentTint }]}>
      {emoji ? (
        <Text style={{ fontSize: Math.round(size * 0.5) }}>{emoji}</Text>
      ) : (
        <Text
          style={[styles.monogramLetter, { fontSize: Math.round(size * 0.5), color: palette.onAgentTint }]}
        >
          {(name.trim()[0] ?? "L").toUpperCase()}
        </Text>
      )}
    </View>
  );
}

/** Static "active" status dot (spec §5 StatusDot), in the scheme's live colour. */
export function AgentStatusDot() {
  const palette = useChatPalette();
  return <View style={[styles.statusDot, { backgroundColor: palette.live }]} />;
}

const styles = StyleSheet.create({
  monogram: {
    borderRadius: radii.md,
    alignItems: "center",
    justifyContent: "center",
  },
  monogramLetter: {
    fontFamily: fonts.loraBoldItalic,
  },
  statusDot: {
    width: 7,
    height: 7,
    borderRadius: 4,
  },
});
