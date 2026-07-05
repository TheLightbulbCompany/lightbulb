import { StyleSheet, Text, View } from "react-native";

// Placeholder — replaced by the Clerk sign-in flow in Phase 1.
export default function SignIn() {
  return (
    <View style={styles.container}>
      <Text style={styles.title}>isol8</Text>
      <Text style={styles.subtitle}>Sign in (Phase 1)</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, alignItems: "center", justifyContent: "center", gap: 8 },
  title: { fontSize: 32, fontWeight: "700" },
  subtitle: { fontSize: 16, opacity: 0.6 },
});
