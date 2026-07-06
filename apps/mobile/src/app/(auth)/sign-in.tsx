import { useSignIn, useSSO } from "@clerk/clerk-expo";
import * as AuthSession from "expo-auth-session";
import * as WebBrowser from "expo-web-browser";
import { useState } from "react";
import {
  ActivityIndicator,
  KeyboardAvoidingView,
  Platform,
  Pressable,
  StyleSheet,
  Text,
  TextInput,
  View,
} from "react-native";

import { colors, fonts, radii, spacing, type } from "../../config/theme";

// Required for the SSO browser round-trip: closes the auth popup and hands
// the redirect result back to the pending startSSOFlow promise.
WebBrowser.maybeCompleteAuthSession();

/** Surface Clerk's first human-readable API error, mirroring the email path. */
function clerkErrorMessage(e: unknown, fallback: string): string {
  if (e instanceof Error && "errors" in e) {
    const errors = (e as { errors?: { longMessage?: string; message?: string }[] }).errors;
    return errors?.[0]?.longMessage ?? errors?.[0]?.message ?? e.message;
  }
  return e instanceof Error ? e.message : fallback;
}

/**
 * Sign-in via Clerk's headless flows: Google SSO or email + password.
 * Deliberately minimal for Phase 1: accounts that require another first
 * factor (email code, MFA) are directed to the web app until those
 * strategies land here.
 */
export default function SignIn() {
  const { signIn, setActive, isLoaded } = useSignIn();
  const { startSSOFlow } = useSSO();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [googleSubmitting, setGoogleSubmitting] = useState(false);
  // Presentational only: which input shows the forest focus border.
  const [focusedField, setFocusedField] = useState<"email" | "password" | null>(null);

  async function onGoogleSignIn() {
    if (!isLoaded || submitting || googleSubmitting) return;
    setGoogleSubmitting(true);
    setError(null);
    try {
      const { createdSessionId, authSessionResult } = await startSSOFlow({
        strategy: "oauth_google",
        redirectUrl: AuthSession.makeRedirectUri(),
      });
      if (createdSessionId) {
        await setActive({ session: createdSessionId });
        // (auth) layout redirects to /(app)/chat once isSignedIn flips.
      } else if (
        authSessionResult?.type === "cancel" ||
        authSessionResult?.type === "dismiss"
      ) {
        // User closed the browser — not an error, stay quiet.
      } else {
        setError(
          "Google sign-in needs another step — finish signing in on isol8.co for now.",
        );
      }
    } catch (e) {
      setError(clerkErrorMessage(e, "Google sign-in failed"));
    } finally {
      setGoogleSubmitting(false);
    }
  }

  async function onSignIn() {
    if (!isLoaded || submitting || googleSubmitting) return;
    setSubmitting(true);
    setError(null);
    try {
      const attempt = await signIn.create({ identifier: email.trim(), password });
      if (attempt.status === "complete") {
        await setActive({ session: attempt.createdSessionId });
        // (auth) layout redirects to /(app)/chat once isSignedIn flips.
      } else {
        setError(
          "This account needs another verification step — sign in on isol8.co for now.",
        );
      }
    } catch (e) {
      const msg =
        e instanceof Error && "errors" in e
          ? // Clerk APIResponseError: surface the first human-readable message
            ((e as { errors?: { longMessage?: string; message?: string }[] }).errors?.[0]?.longMessage ??
              (e as { errors?: { message?: string }[] }).errors?.[0]?.message ??
              e.message)
          : e instanceof Error
            ? e.message
            : "Sign in failed";
      setError(msg);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <KeyboardAvoidingView
      style={styles.container}
      behavior={Platform.OS === "ios" ? "padding" : undefined}
    >
      <Text style={styles.logo}>isol8</Text>
      <Pressable
        style={[
          styles.googleButton,
          (!isLoaded || submitting || googleSubmitting) && styles.buttonDisabled,
        ]}
        onPress={onGoogleSignIn}
        disabled={!isLoaded || submitting || googleSubmitting}
      >
        {googleSubmitting ? (
          <ActivityIndicator color={colors.forest} />
        ) : (
          <>
            <Text style={styles.googleMark}>G</Text>
            <Text style={styles.googleButtonText}>Continue with Google</Text>
          </>
        )}
      </Pressable>
      <View style={styles.divider}>
        <View style={styles.dividerRule} />
        <Text style={styles.dividerLabel}>or</Text>
        <View style={styles.dividerRule} />
      </View>
      <TextInput
        style={[styles.input, focusedField === "email" && styles.inputFocused]}
        placeholder="Email"
        placeholderTextColor={colors.muted}
        autoCapitalize="none"
        autoComplete="email"
        keyboardType="email-address"
        editable={!googleSubmitting}
        value={email}
        onChangeText={setEmail}
        onFocus={() => setFocusedField("email")}
        onBlur={() => setFocusedField(null)}
      />
      <TextInput
        style={[styles.input, focusedField === "password" && styles.inputFocused]}
        placeholder="Password"
        placeholderTextColor={colors.muted}
        secureTextEntry
        autoComplete="current-password"
        editable={!googleSubmitting}
        value={password}
        onChangeText={setPassword}
        onFocus={() => setFocusedField("password")}
        onBlur={() => setFocusedField(null)}
      />
      {error ? <Text style={styles.error}>{error}</Text> : null}
      <Pressable
        style={[
          styles.button,
          (!isLoaded || submitting || googleSubmitting) && styles.buttonDisabled,
        ]}
        onPress={onSignIn}
        disabled={!isLoaded || submitting || googleSubmitting}
      >
        {submitting ? (
          <ActivityIndicator color={colors.white} />
        ) : (
          <Text style={styles.buttonText}>Sign in</Text>
        )}
      </Pressable>
      <Text style={styles.hint}>New here? Create your account at isol8.co</Text>
    </KeyboardAvoidingView>
  );
}

const styles = StyleSheet.create({
  container: {
    flex: 1,
    justifyContent: "center",
    padding: spacing.xl,
    gap: spacing.md,
    backgroundColor: colors.cream,
  },
  // Lora wordmark, mirroring the web landing masthead.
  logo: {
    ...type.display,
    fontFamily: fonts.loraBold,
    textAlign: "center",
    marginBottom: spacing.xl,
  },
  googleButton: {
    flexDirection: "row",
    justifyContent: "center",
    alignItems: "center",
    gap: spacing.sm,
    backgroundColor: colors.white,
    borderWidth: 1,
    borderColor: colors.border,
    borderRadius: radii.md,
    paddingVertical: spacing.md,
  },
  // Inline "G" mark: a serif glyph in the accent color — no image asset.
  googleMark: {
    fontFamily: fonts.loraBold,
    fontSize: 17,
    lineHeight: 22,
    color: colors.forest,
  },
  googleButtonText: {
    color: colors.ink,
    fontFamily: fonts.dmSansMedium,
    fontSize: 15,
    lineHeight: 22,
  },
  divider: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.md,
  },
  dividerRule: {
    flex: 1,
    height: StyleSheet.hairlineWidth,
    backgroundColor: colors.border,
  },
  dividerLabel: { ...type.caption },
  input: {
    backgroundColor: colors.white,
    borderWidth: 1,
    borderColor: colors.border,
    borderRadius: radii.md,
    paddingHorizontal: spacing.md,
    paddingVertical: spacing.md,
    fontFamily: fonts.dmSans,
    fontSize: 15,
    color: colors.ink,
  },
  inputFocused: { borderColor: colors.forest },
  button: {
    backgroundColor: colors.forest,
    borderRadius: radii.md,
    paddingVertical: spacing.md,
    alignItems: "center",
    marginTop: spacing.xs,
  },
  buttonDisabled: { opacity: 0.5 },
  buttonText: {
    color: colors.white,
    fontFamily: fonts.dmSansMedium,
    fontSize: 15,
    lineHeight: 22,
  },
  error: { ...type.caption, color: colors.error },
  hint: { ...type.caption, textAlign: "center", marginTop: spacing.md },
});
