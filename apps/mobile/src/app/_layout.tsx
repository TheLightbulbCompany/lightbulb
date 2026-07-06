import { ClerkProvider } from "@clerk/clerk-expo";
import { Stack } from "expo-router";
import { StatusBar } from "expo-status-bar";
import { StyleSheet, Text, View } from "react-native";

import { tokenCache } from "../lib/tokenCache";

const publishableKey = process.env.EXPO_PUBLIC_CLERK_PUBLISHABLE_KEY;

export default function RootLayout() {
  // Fail soft: a missing key renders instructions instead of ClerkProvider's
  // hard throw, so a fresh checkout still boots in Expo Go.
  if (!publishableKey) {
    return (
      <View style={styles.center}>
        <Text style={styles.title}>Missing Clerk key</Text>
        <Text style={styles.body}>
          Set EXPO_PUBLIC_CLERK_PUBLISHABLE_KEY (dev instance key) and restart
          Metro. See apps/mobile/README.md.
        </Text>
      </View>
    );
  }

  return (
    <ClerkProvider publishableKey={publishableKey} tokenCache={tokenCache}>
      <Stack screenOptions={{ headerShown: false }}>
        <Stack.Screen name="(auth)" />
        <Stack.Screen name="(app)" />
      </Stack>
      <StatusBar style="auto" />
    </ClerkProvider>
  );
}

const styles = StyleSheet.create({
  center: { flex: 1, alignItems: "center", justifyContent: "center", gap: 8, padding: 32 },
  title: { fontSize: 20, fontWeight: "700" },
  body: { fontSize: 14, opacity: 0.7, textAlign: "center" },
});
