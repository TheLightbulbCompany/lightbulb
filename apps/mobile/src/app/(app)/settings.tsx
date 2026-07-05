import { StyleSheet, Text, View } from "react-native";

// Placeholder — replaced by profile/channels/keys/integrations in Phase 6.
export default function Settings() {
  return (
    <View style={styles.container}>
      <Text style={styles.heading}>Settings</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, alignItems: "center", justifyContent: "center" },
  heading: { fontSize: 24, fontWeight: "600" },
});
