import { StyleSheet, Text, View } from "react-native";

import { colors, spacing, type } from "../../config/theme";

// Placeholder — replaced by the Teams inbox surface in Phase 5.
export default function Teams() {
  return (
    <View style={styles.container}>
      <Text style={styles.heading}>Teams</Text>
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
