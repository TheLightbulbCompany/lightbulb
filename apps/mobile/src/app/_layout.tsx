import { ClerkProvider } from "@clerk/clerk-expo";
import {
  Lora_400Regular,
  Lora_400Regular_Italic,
  Lora_500Medium,
  Lora_700Bold,
  Lora_700Bold_Italic,
} from "@expo-google-fonts/lora";
import {
  SourceSans3_400Regular,
  SourceSans3_500Medium,
  SourceSans3_600SemiBold,
} from "@expo-google-fonts/source-sans-3";
import { useFonts } from "expo-font";
import { Stack } from "expo-router";
import * as SplashScreen from "expo-splash-screen";
import { StatusBar } from "expo-status-bar";
import { useEffect } from "react";
import { StyleSheet, Text, View } from "react-native";

import { colors, spacing, type } from "../config/theme";
import { tokenCache } from "../lib/tokenCache";

const publishableKey = process.env.EXPO_PUBLIC_CLERK_PUBLISHABLE_KEY;

// Hold the splash screen until fonts are ready so the first frame renders in
// Source Sans 3 / Lora rather than flashing system fonts.
SplashScreen.preventAutoHideAsync().catch(() => {
  // Reloads / fast refresh can reject this; the splash is already hidden.
});

export default function RootLayout() {
  const [fontsLoaded, fontError] = useFonts({
    SourceSans3_400Regular,
    SourceSans3_500Medium,
    SourceSans3_600SemiBold,
    Lora_400Regular,
    Lora_400Regular_Italic,
    Lora_500Medium,
    Lora_700Bold,
    Lora_700Bold_Italic,
  });
  // On font-load failure we still proceed — unknown fontFamily names fall
  // back to system fonts. Never leave the user on a blank screen.
  const ready = fontsLoaded || fontError != null;

  useEffect(() => {
    if (ready) {
      SplashScreen.hideAsync().catch(() => {});
    }
  }, [ready]);

  if (!ready) return null; // Native splash is still covering the screen.

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
      <Stack
        screenOptions={{
          headerShown: false,
          contentStyle: { backgroundColor: colors.cream },
        }}
      >
        <Stack.Screen name="(auth)" />
        <Stack.Screen name="(app)" />
      </Stack>
      <StatusBar style="dark" />
    </ClerkProvider>
  );
}

const styles = StyleSheet.create({
  center: {
    flex: 1,
    alignItems: "center",
    justifyContent: "center",
    gap: spacing.sm,
    padding: spacing.xl + spacing.sm,
    backgroundColor: colors.cream,
  },
  title: { ...type.title },
  body: { ...type.body, color: colors.mutedSecondary, textAlign: "center" },
});
