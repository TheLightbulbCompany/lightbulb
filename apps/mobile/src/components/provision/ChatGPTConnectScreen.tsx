/**
 * In-app ChatGPT device-code sign-in (mobile onboarding-parity Slice 4).
 *
 * Rendered by ProvisionGate when the container-status gate reports
 * `blocked.code === "oauth_required"` — the owner already picked ChatGPT as
 * their provider but hasn't completed the device-code handshake. Mirrors the
 * web ChatGPTOAuthStep: the shared useChatGPTOAuth hook owns the on-mount
 * status probe + auto-start + poll loop (see the hook's own doc comment) —
 * this screen never calls start() itself except from the error state's
 * "Try again" retry. The device-code flow has no redirect back to the app,
 * so the verification URL opens via openBrowserAsync (NOT
 * openAuthSessionAsync, which would wait forever for a return URL).
 */
import { useAuth } from "@clerk/clerk-expo";
import { useCallback, useEffect, useMemo, useRef } from "react";
import { ActivityIndicator, Alert, Pressable, StyleSheet, Text, View } from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import * as WebBrowser from "expo-web-browser";
import { createApiClient, useChatGPTOAuth } from "@isol8/shared";

import { colors, fonts, radii, spacing, type } from "../../config/theme";
import { makeAdapter } from "../../lib/adapter";

interface Props {
  /** Fired once state.status flips to "completed" so the gate re-polls. */
  onConnected: () => void;
}

export function ChatGPTConnectScreen({ onConnected }: Props) {
  const insets = useSafeAreaInsets();
  const { getToken, signOut } = useAuth();
  const getTokenRef = useRef(getToken);
  getTokenRef.current = getToken;
  const api = useMemo(
    () => createApiClient(makeAdapter(() => getTokenRef.current())),
    [],
  );

  const { state, start } = useChatGPTOAuth(api);

  // This is a full-screen takeover with no other navigation — a wrong-account
  // or otherwise-stuck user needs an escape hatch. Same confirm-then-signOut
  // pattern as ProvisionGate's Interstitial (the every-other-blocked-state
  // screen this one replaces for the oauth_required gate).
  const onSignOut = useCallback(() => {
    Alert.alert("Sign out", "Sign out of this account?", [
      { text: "Cancel", style: "cancel" },
      { text: "Sign out", style: "destructive", onPress: () => void signOut() },
    ]);
  }, [signOut]);

  useEffect(() => {
    if (state.status === "completed") onConnected();
  }, [state, onConnected]);

  const openVerificationUrl = useCallback(() => {
    if (state.status === "pending") {
      void WebBrowser.openBrowserAsync(state.verificationUri);
    }
  }, [state]);

  let body: React.ReactNode;
  if (state.status === "pending") {
    body = (
      <>
        <Text style={styles.headline}>Almost there</Text>
        <Text style={styles.body}>
          Open the sign-in page below and sign in to your ChatGPT account,
          then enter this code.
        </Text>
        <View style={styles.codeBox}>
          <Text style={styles.code}>{state.userCode}</Text>
        </View>
        <Pressable
          style={({ pressed }) => [styles.primaryButton, pressed && styles.pressed]}
          onPress={openVerificationUrl}
          accessibilityRole="button"
          accessibilityLabel="Open sign-in page"
        >
          <Text style={styles.primaryButtonText}>Open sign-in page</Text>
        </Pressable>
        <Text style={styles.urlCaption} numberOfLines={1}>
          {state.verificationUri}
        </Text>
        <Text style={styles.waiting}>Waiting for you to complete sign-in…</Text>
      </>
    );
  } else if (state.status === "error") {
    body = (
      <>
        <Text style={styles.errorText}>Connection failed: {state.message}</Text>
        <Pressable
          style={({ pressed }) => [styles.primaryButton, pressed && styles.pressed]}
          onPress={() => void start()}
          accessibilityRole="button"
          accessibilityLabel="Try again"
        >
          <Text style={styles.primaryButtonText}>Try again</Text>
        </Pressable>
      </>
    );
  } else if (state.status === "completed") {
    // Transient — the useEffect above fires onConnected() the same tick,
    // so the parent gate normally swaps this screen out immediately.
    body = <Text style={styles.body}>Connected — finishing up…</Text>;
  } else {
    // idle — auto-start hasn't landed yet (one render frame).
    body = (
      <>
        <ActivityIndicator size="small" color={colors.forest} />
        <Text style={styles.waiting}>Starting ChatGPT sign-in…</Text>
      </>
    );
  }

  return (
    <View style={[styles.page, { paddingTop: insets.top }]}>
      <View style={styles.card}>
        {body}
        <Pressable
          style={({ pressed }) => [styles.signOutButton, pressed && styles.pressed]}
          onPress={onSignOut}
          accessibilityRole="button"
          accessibilityLabel="Sign out"
        >
          <Text style={styles.signOutText}>Sign out</Text>
        </Pressable>
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  page: {
    flex: 1,
    backgroundColor: colors.cream,
    alignItems: "center",
    justifyContent: "center",
    padding: spacing.xl,
  },
  card: {
    width: "100%",
    maxWidth: 400,
    alignItems: "center",
    gap: spacing.md,
    backgroundColor: colors.white,
    borderRadius: radii.lg,
    borderWidth: 1,
    borderColor: colors.edge,
    padding: spacing.xl,
  },
  headline: {
    ...type.title,
    fontSize: 18,
    lineHeight: 24,
    textAlign: "center",
  },
  body: {
    fontFamily: fonts.dmSans,
    fontSize: 14,
    lineHeight: 20,
    color: colors.mute,
    textAlign: "center",
  },
  codeBox: {
    alignSelf: "stretch",
    backgroundColor: colors.creamDeeper,
    borderRadius: radii.md,
    paddingVertical: spacing.md,
    alignItems: "center",
  },
  code: {
    fontFamily: fonts.mono,
    fontSize: 20,
    letterSpacing: 2,
    color: colors.ink,
  },
  primaryButton: {
    alignSelf: "stretch",
    alignItems: "center",
    justifyContent: "center",
    paddingVertical: spacing.md,
    borderRadius: radii.md,
    backgroundColor: colors.forest,
  },
  primaryButtonText: {
    fontFamily: fonts.dmSansMedium,
    fontSize: 15,
    lineHeight: 20,
    color: colors.white,
  },
  urlCaption: {
    fontFamily: fonts.dmSans,
    fontSize: 11,
    lineHeight: 15,
    color: colors.mute,
  },
  waiting: {
    fontFamily: fonts.dmSans,
    fontSize: 13,
    lineHeight: 18,
    color: colors.mute,
    textAlign: "center",
  },
  errorText: {
    fontFamily: fonts.dmSans,
    fontSize: 14,
    lineHeight: 20,
    color: colors.errorText,
    textAlign: "center",
  },
  pressed: { opacity: 0.6 },
  // Matches ProvisionGate's Interstitial signOutButton/signOutText exactly.
  signOutButton: {
    alignSelf: "center",
    paddingHorizontal: spacing.lg,
    paddingVertical: spacing.xs,
  },
  signOutText: {
    fontFamily: fonts.dmSans,
    fontSize: 13,
    lineHeight: 17,
    color: colors.mute,
  },
});
