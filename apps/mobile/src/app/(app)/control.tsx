import { StyleSheet, Text, View } from "react-native";

import { colors, spacing, type } from "../../config/theme";

// Placeholder — replaced by the user-facing control panels in Phase 4.
export default function Control() {
  return (
    <View style={styles.container}>
      <Text style={styles.heading}>Control</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  container: {
    flex: 1,
    alignItems: "center",
    justifyContent: "center",
    gap: spacing.sm,
    backgroundColor: colors.cream,
  },
  heading: { ...type.display },
});
