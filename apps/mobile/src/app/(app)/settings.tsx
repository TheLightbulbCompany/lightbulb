import { useAuth, useUser } from "@clerk/clerk-expo";
import { Pressable, StyleSheet, Text, View } from "react-native";

// Placeholder — full settings parity lands in Phase 6. Sign-out lives here
// from day one so testers can switch accounts without reinstalling.
export default function Settings() {
  const { signOut } = useAuth();
  const { user } = useUser();

  return (
    <View style={styles.container}>
      <Text style={styles.heading}>Settings</Text>
      <Text style={styles.email}>{user?.primaryEmailAddress?.emailAddress ?? ""}</Text>
      <Pressable style={styles.button} onPress={() => signOut()}>
        <Text style={styles.buttonText}>Sign out</Text>
      </Pressable>
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, alignItems: "center", justifyContent: "center", gap: 10 },
  heading: { fontSize: 24, fontWeight: "600" },
  email: { fontSize: 14, opacity: 0.6 },
  button: {
    borderWidth: 1,
    borderColor: "#b3261e",
    borderRadius: 8,
    paddingHorizontal: 18,
    paddingVertical: 10,
    marginTop: 8,
  },
  buttonText: { color: "#b3261e", fontSize: 14, fontWeight: "600" },
});
