import { StyleSheet, Text, View } from "react-native";

import { API_URL, WS_URL } from "../../config/env";

// Placeholder — replaced by the agent list + chat timeline in Phase 2.
export default function Chat() {
  return (
    <View style={styles.container}>
      <Text style={styles.heading}>Chat</Text>
      <Text style={styles.meta}>API: {API_URL}</Text>
      <Text style={styles.meta}>WS: {WS_URL}</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, alignItems: "center", justifyContent: "center", gap: 6, padding: 24 },
  heading: { fontSize: 24, fontWeight: "600" },
  meta: { fontSize: 12, opacity: 0.5 },
});
