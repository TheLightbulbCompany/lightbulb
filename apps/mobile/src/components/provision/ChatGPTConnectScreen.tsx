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
import { useAuth } from "@clerk/expo";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ActivityIndicator, Alert, Pressable, StyleSheet, Text, View } from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import * as WebBrowser from "expo-web-browser";
import { createApiClient, useChatGPTOAuth } from "@lightbulb/shared";

import { colors, fonts, radii, spacing } from "../../config/theme";
import { makeAdapter } from "../../lib/adapter";
import { Wordmark } from "./Wordmark";

/**
 * Whole seconds until `expiresAt` (epoch ms), re-derived once a second, or
 * `null` when there's no live code. The device code silently dies at ~15 min
 * (useChatGPTOAuth sets `Date.now() + expires_in * 1000`); without surfacing
 * this the user only learns it expired via a later poll error. It's a plain
 * numeric text tick — no Animated/transform — so there's nothing here to gate
 * on reduced motion (a once-a-second digit change isn't vestibular motion).
 */
function useSecondsRemaining(expiresAt: number | null): number | null {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (expiresAt == null) return;
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, [expiresAt]);
  if (expiresAt == null) return null;
  return Math.max(0, Math.ceil((expiresAt - now) / 1000));
}

/** Whole seconds → "M:SS". */
function formatCountdown(totalSeconds: number): string {
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${minutes}:${String(seconds).padStart(2, "0")}`;
}

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

  // ~15-min device-code lifetime, surfaced so the user isn't typing a code
  // that already died. Only "pending" carries an expiry; every other status
  // passes null and the hook idles.
  const secondsRemaining = useSecondsRemaining(
    state.status === "pending" ? state.expiresAt : null,
  );

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
          {/* selectable → long-press exposes the OS "Copy" affordance. There's
              no expo-clipboard binary in this build and adding one would break
              OTA delivery, so this is the copy fix — the verification page
              opens in a modal that covers this screen, so the user must be
              able to lift the code without retyping it. */}
          <Text style={styles.code} selectable>
            {state.userCode}
          </Text>
        </View>
        {secondsRemaining !== null ? (
          <Text
            style={[
              styles.expiry,
              secondsRemaining === 0 && styles.expiryExpired,
            ]}
          >
            {secondsRemaining > 0
              ? `Code expires in ${formatCountdown(secondsRemaining)}`
              : "Code expired — tap Try again"}
          </Text>
        ) : null}
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
  } else if (state.status === "plan_required") {
    // /start refused before any OpenAI sign-in began: the workspace has no paid plan yet.
    body = (
      <>
        <Text style={styles.body}>Signing in with ChatGPT comes with the Workspace plan.</Text>
        <Text style={styles.waiting}>Choose the plan in Settings → Billing, then come back here to sign in.</Text>
        <Pressable
          style={({ pressed }) => [styles.primaryButton, pressed && styles.pressed]}
          onPress={() => void start()}
          accessibilityRole="button"
          accessibilityLabel="I have the plan, try again"
        >
          <Text style={styles.primaryButtonText}>I have the plan — try again</Text>
        </Pressable>
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
        <Wordmark />
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
  // Regular weight, not type.title's bold — matches web's `.provision-title`
  // (Satoshi 400) so the onboarding surfaces read the same on both clients.
  headline: {
    fontFamily: fonts.lora,
    fontSize: 20,
    lineHeight: 26,
    color: colors.ink,
    textAlign: "center",
  },
  body: {
    fontFamily: fonts.sans,
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
  expiry: {
    fontFamily: fonts.sans,
    fontSize: 12,
    lineHeight: 16,
    color: colors.mute,
    textAlign: "center",
  },
  expiryExpired: {
    color: colors.warn,
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
    fontFamily: fonts.sansMedium,
    fontSize: 15,
    lineHeight: 20,
    color: colors.white,
  },
  urlCaption: {
    fontFamily: fonts.sans,
    fontSize: 11,
    lineHeight: 15,
    color: colors.mute,
  },
  waiting: {
    fontFamily: fonts.sans,
    fontSize: 13,
    lineHeight: 18,
    color: colors.mute,
    textAlign: "center",
  },
  errorText: {
    fontFamily: fonts.sans,
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
    fontFamily: fonts.sans,
    fontSize: 13,
    lineHeight: 17,
    color: colors.mute,
  },
});
