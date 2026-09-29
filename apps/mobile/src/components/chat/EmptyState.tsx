import { StyleSheet, Text, View } from "react-native";

import { fonts, spacing, type ChatPalette } from "../../config/theme";
import { LogoMark } from "../LogoMark";

/**
 * Empty-conversation hero (spec §7): the centred bulb mark, the conversation's
 * name in Lora, and a one-line prompt. The default prompt reads as an agent DM;
 * a channel or group DM passes its own.
 */
export function EmptyState({
  agentName,
  subtitle = "Start a conversation with your agent",
  palette,
}: {
  agentName: string;
  subtitle?: string;
  palette: ChatPalette;
}) {
  return (
    <View style={styles.wrap}>
      <LogoMark size={57} />
      <Text style={[styles.name, { color: palette.fg }]} numberOfLines={2}>
        {agentName}
      </Text>
      <Text style={[styles.sub, { color: palette.fgMute }]}>{subtitle}</Text>
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
  name: {
    fontFamily: fonts.lora,
    fontSize: 30,
    lineHeight: 38,
    letterSpacing: -0.75,
    textAlign: "center",
  },
  sub: {
    fontFamily: fonts.sans,
    fontSize: 16,
    lineHeight: 24,
    textAlign: "center",
  },
});
