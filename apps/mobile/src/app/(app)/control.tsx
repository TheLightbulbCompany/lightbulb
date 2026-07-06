import { StyleSheet, Text, View } from "react-native";

// Placeholder — replaced by the user-facing control panels in Phase 4.
export default function Control() {
  return (
    <View style={styles.container}>
      <Text style={styles.heading}>Control</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, alignItems: "center", justifyContent: "center" },
  heading: { fontSize: 24, fontWeight: "600" },
});
