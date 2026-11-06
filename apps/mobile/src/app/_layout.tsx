import { ClerkProvider } from "@clerk/expo";
import { useFonts } from "expo-font";
import { DarkTheme, DefaultTheme, Stack, ThemeProvider } from "expo-router";
import * as SplashScreen from "expo-splash-screen";
import { StatusBar } from "expo-status-bar";
import { useEffect } from "react";
import { StyleSheet, Text, View, useColorScheme } from "react-native";
import { GestureHandlerRootView } from "react-native-gesture-handler";
import { KeyboardProvider } from "react-native-keyboard-controller";

import { chatPalette, colors, spacing, type } from "../config/theme";
import { tokenCache } from "../lib/tokenCache";

/**
 * The one React Navigation theme for the whole app. Without this every
 * native-stack header renders React Navigation's own light default — white
 * bar, near-invisible title — in dark mode, since nothing here ever told it
 * about `chatPalette`. `fonts` comes from the base theme unchanged: nav
 * chrome deliberately stays on the system font (see `type.navTitle`).
 */
function useNavigationTheme() {
  const dark = useColorScheme() === "dark";
  const palette = dark ? chatPalette.dark : chatPalette.light;
  const base = dark ? DarkTheme : DefaultTheme;
  return {
    ...base,
    dark,
    colors: {
      ...base.colors,
      background: palette.ground,
      card: palette.surface,
      text: palette.fg,
      border: palette.edge,
      primary: palette.link,
      notification: palette.alert,
    },
  };
}

const publishableKey = process.env.EXPO_PUBLIC_CLERK_PUBLISHABLE_KEY;

// Hold the splash screen until fonts are ready so the first frame renders in
// Satoshi rather than flashing system fonts.
SplashScreen.preventAutoHideAsync().catch(() => {
  // Reloads / fast refresh can reject this; the splash is already hidden.
});

export default function RootLayout() {
  const [fontsLoaded, fontError] = useFonts({
    // Keys are the family names `fonts` in config/theme.ts passes around.
    "Satoshi-Regular": require("../../assets/fonts/satoshi/Satoshi-Regular.ttf"),
    "Satoshi-Medium": require("../../assets/fonts/satoshi/Satoshi-Medium.ttf"),
    "Satoshi-Bold": require("../../assets/fonts/satoshi/Satoshi-Bold.ttf"),
    "Satoshi-Italic": require("../../assets/fonts/satoshi/Satoshi-Italic.ttf"),
    "Satoshi-BoldItalic": require("../../assets/fonts/satoshi/Satoshi-BoldItalic.ttf"),
  });
  // On font-load failure we still proceed — unknown fontFamily names fall
  // back to system fonts. Never leave the user on a blank screen.
  const ready = fontsLoaded || fontError != null;
  const navTheme = useNavigationTheme();

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

  // KeyboardProvider is the native keyboard observer every composer on the two
  // room screens rides. It has to sit above the navigator: it installs ONE
  // UIKit keyboard-layout observer for the whole app, and the shared values it
  // publishes are what let the composer track an interactive dismiss drag
  // frame-for-frame (RN's own Keyboard events only fire at the ends).
  //
  // GestureHandlerRootView is where gesture-handler recognizes gestures at all
  // (the conversation list's swipe actions); expo-router mounts none itself.
  return (
    <GestureHandlerRootView style={styles.root}>
      <KeyboardProvider>
        <ClerkProvider publishableKey={publishableKey} tokenCache={tokenCache}>
          {/* Dark by default for the light-only screens outside the app proper
              (sign-in, the consent/workspace/provisioning gates). The (app)
              layout mounts its own `auto` entry once past those gates, where
              every header follows the navigation theme. Mounts BEFORE the Stack:
              entries merge in mount order and the last one wins. */}
          <StatusBar style="dark" />
          <ThemeProvider value={navTheme}>
            <Stack
              screenOptions={{
                headerShown: false,
                contentStyle: { backgroundColor: navTheme.colors.background },
              }}
            >
              <Stack.Screen name="(auth)" />
              <Stack.Screen name="(app)" />
            </Stack>
          </ThemeProvider>
        </ClerkProvider>
      </KeyboardProvider>
    </GestureHandlerRootView>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1 },
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
