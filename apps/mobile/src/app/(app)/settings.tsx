import { useAuth, useUser } from "@clerk/clerk-expo";
import { Pressable, StyleSheet, Text, View } from "react-native";

import { colors, fonts, radii, spacing, type } from "../../config/theme";

// Placeholder — full settings parity lands in Phase 6. Sign-out lives here
// from day one so testers can switch accounts without reinstalling.
export default function Settings() {
  const { signOut } = useAuth();
  const { user } = useUser();

  return (
    <View style={styles.container}>
      <Text style={styles.heading}>Settings</Text>
      <Text style={styles.email}>{user?.primaryEmailAddress?.emailAddress ?? ""}</Text>
      <Pressable
        style={({ pressed }) => [styles.button, pressed && styles.buttonPressed]}
        onPress={() => signOut()}
      >
        <Text style={styles.buttonText}>Sign out</Text>
      </Pressable>
    </View>
  );
}

const styles = StyleSheet.create({
  container: {
    flex: 1,
    alignItems: "center",
    justifyContent: "center",
    gap: spacing.sm + 2,
    backgroundColor: colors.cream,
  },
  heading: { ...type.display },
  email: { ...type.body, color: colors.mutedSecondary },
  // Muted-destructive outline, matching the web's danger pattern.
  button: {
    borderWidth: 1,
    borderColor: colors.border,
    borderRadius: radii.sm,
    backgroundColor: colors.white,
    paddingHorizontal: spacing.lg + 2,
    paddingVertical: spacing.sm + 2,
    marginTop: spacing.sm,
  },
  buttonPressed: { backgroundColor: colors.creamHover },
  buttonText: {
    color: colors.error,
    fontFamily: fonts.dmSansMedium,
    fontSize: 14,
  },
});
