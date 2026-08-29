import { Animated, StyleSheet, Text, View } from "react-native";

import { colors, fonts, spacing } from "../../config/theme";
import { LogoMark } from "../LogoMark";
import { usePulse } from "./usePulse";

/**
 * The bulb mark that heads assistant messages (spec §1/§7): static when
 * idle, a gentle 1.8s opacity pulse while the agent is thinking/streaming.
 * The pulse respects the system reduce-motion setting via usePulse.
 */
export function AgentGlyph({ active }: { active: boolean }) {
  const pulse = usePulse(active, 0.3, 900);
  return (
    <Animated.View style={[styles.glyphBox, { opacity: pulse }]}>
      <LogoMark size={20} />
    </Animated.View>
  );
}

/**
 * Header row above each assistant message group (spec §1 AgentHead): agent
 * display name + the activity glyph. Web's hover-reveal action buttons
 * (copy/refresh/share) are deferred — no hover on touch.
 */
export function AgentHead({ name, active }: { name: string; active: boolean }) {
  return (
    <View style={styles.head}>
      <Text style={styles.name} numberOfLines={1}>
        {name}
      </Text>
      <AgentGlyph active={active} />
    </View>
  );
}

const styles = StyleSheet.create({
  head: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.sm,
    marginBottom: spacing.sm,
  },
  name: {
    fontFamily: fonts.sansSemiBold,
    fontSize: 12,
    lineHeight: 16,
    letterSpacing: -0.3,
    color: colors.assistantInk,
    flexShrink: 1,
  },
  glyphBox: {
    width: 20,
    height: 20,
    alignItems: "center",
    justifyContent: "center",
  },
});
