import { StyleSheet, Text, View } from "react-native";

import { colors, fonts, spacing } from "../../config/theme";

// Placeholder for the Control panels not built in W3-C1 (agents/skills/
// sessions/channels/usage/llm/integrations). The Stack header (control/
// _layout.tsx) supplies the panel title; this fills the body so navigation
// never dead-ends. C2..C4 replace these screens with real panels.
export function StubScreen() {
  return (
    <View style={styles.page}>
      <Text style={styles.body}>Coming in this build.</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  page: {
    flex: 1,
    backgroundColor: colors.cream,
    alignItems: "center",
    justifyContent: "center",
    padding: spacing.xl,
  },
  body: {
    fontFamily: fonts.sans,
    fontSize: 13,
    lineHeight: 18,
    color: colors.mute,
    textAlign: "center",
  },
});
