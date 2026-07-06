import { StyleSheet, Text, View } from "react-native";

// Placeholder — replaced by the Teams inbox surface in Phase 5.
export default function Teams() {
  return (
    <View style={styles.container}>
      <Text style={styles.heading}>Teams</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, alignItems: "center", justifyContent: "center" },
  heading: { fontSize: 24, fontWeight: "600" },
});
