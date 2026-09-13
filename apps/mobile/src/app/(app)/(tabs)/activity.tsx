import { StyleSheet, Text, View } from "react-native";

import { LogoMark } from "../../../components/LogoMark";
import { colors, spacing, type } from "../../../config/theme";

/**
 * Mentions of you and replies to threads you posted in. Phase B owns the feed
 * — the tab exists now so the bar has its final shape and push routing has a
 * destination; there is deliberately no data fetching here.
 */
export default function Activity() {
  return (
    <View style={styles.page}>
      <LogoMark size={48} />
      <Text style={styles.dim}>Mentions and thread replies land here.</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  page: {
    flex: 1,
    alignItems: "center",
    justifyContent: "center",
    gap: spacing.sm,
    padding: spacing.xl,
    backgroundColor: colors.cream,
  },
  dim: { ...type.body, color: colors.mute, textAlign: "center" },
});
