import { useClerk, useSignIn, useSignUp, useSSO } from "@clerk/clerk-expo";
import * as AppleAuthentication from "expo-apple-authentication";
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
import { keepAppleName, signInWithApple } from "../../lib/appleSignIn";

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
  const { signUp } = useSignUp();
  const clerk = useClerk();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [googleSubmitting, setGoogleSubmitting] = useState(false);
  const [appleSubmitting, setAppleSubmitting] = useState(false);
  // Presentational only: which input shows the forest focus border.
  const [focusedField, setFocusedField] = useState<"email" | "password" | null>(null);

  const anySubmitting = submitting || googleSubmitting || appleSubmitting;

  // Native Sign in with Apple (guideline 4.8 — required alongside Google).
  // lib/appleSignIn drives expo-apple-authentication and Clerk's sign-in↔sign-up
  // transfer, and keeps the name Apple shares once; iOS-only by platform gate below.
  async function onAppleSignIn() {
    if (!isLoaded || !signUp || anySubmitting) return;
    setAppleSubmitting(true);
    setError(null);
    try {
      const { createdSessionId, name } = await signInWithApple(signIn, signUp);
      if (createdSessionId) {
        await setActive({ session: createdSessionId });
        // (auth) layout redirects to /(app)/home once isSignedIn flips.
        await keepAppleName(clerk.user, name);
      }
    } catch (e) {
      // The native sheet's cancel surfaces as ERR_REQUEST_CANCELED — quiet.
      if ((e as { code?: string })?.code !== "ERR_REQUEST_CANCELED") {
        setError(clerkErrorMessage(e, "Apple sign-in failed"));
      }
    } finally {
      setAppleSubmitting(false);
    }
  }

  async function onGoogleSignIn() {
    if (!isLoaded || anySubmitting) return;
    setGoogleSubmitting(true);
    setError(null);
    try {
      const { createdSessionId, authSessionResult } = await startSSOFlow({
        strategy: "oauth_google",
        // The explicit path matches Clerk's documented native pattern AND the
        // prod instance's existing allowlist entry (flickmeon://oauth-native-callback).
        // Expo Go yields exp://<host>:8081/--/oauth-native-callback; dev/release
        // builds yield flickmeon://oauth-native-callback.
        redirectUrl: AuthSession.makeRedirectUri({ path: "oauth-native-callback" }),
      });
      if (createdSessionId) {
        await setActive({ session: createdSessionId });
        // (auth) layout redirects to /(app)/home once isSignedIn flips.
      } else if (
        authSessionResult?.type === "cancel" ||
        authSessionResult?.type === "dismiss"
      ) {
        // User closed the browser — not an error, stay quiet.
      } else {
        setError(
          "Google sign-in needs another step — finish signing in on thelightbulb.company for now.",
        );
      }
    } catch (e) {
      setError(clerkErrorMessage(e, "Google sign-in failed"));
    } finally {
      setGoogleSubmitting(false);
    }
  }

  async function onSignIn() {
    if (!isLoaded || anySubmitting) return;
    setSubmitting(true);
    setError(null);
    try {
      const attempt = await signIn.create({ identifier: email.trim(), password });
      if (attempt.status === "complete") {
        await setActive({ session: attempt.createdSessionId });
        // (auth) layout redirects to /(app)/home once isSignedIn flips.
      } else {
        setError(
          "This account needs another verification step — sign in on thelightbulb.company for now.",
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
      <Text style={styles.logo}>Lightbulb</Text>
      {/* Apple's own button component, NOT a look-alike. Rejected under
          guideline 4 (2026-08-12) for drawing the mark as a U+F8FF text glyph:
          the logo artwork has to come from Apple, which for React Native means
          AppleAuthenticationButton (it renders the native ASAuthorization
          button). Its label/artwork/localization are Apple's — the only
          styling we own is type, corner radius, and size.
          While the native sheet is up it is swapped for a same-sized busy
          placeholder: the native button has no `disabled` prop, so this is
          also what stops a second tap racing the first. */}
      {Platform.OS === "ios" &&
        (appleSubmitting ? (
          <View style={[styles.appleButton, styles.appleButtonBusy]}>
            <ActivityIndicator color={colors.white} />
          </View>
        ) : (
          <AppleAuthentication.AppleAuthenticationButton
            buttonType={
              AppleAuthentication.AppleAuthenticationButtonType.CONTINUE
            }
            buttonStyle={
              AppleAuthentication.AppleAuthenticationButtonStyle.BLACK
            }
            cornerRadius={radii.lg}
            style={styles.appleButton}
            onPress={onAppleSignIn}
          />
        ))}
      <Pressable
        style={[
          styles.googleButton,
          (!isLoaded || anySubmitting) && styles.buttonDisabled,
        ]}
        onPress={onGoogleSignIn}
        disabled={!isLoaded || anySubmitting}
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
        editable={!googleSubmitting && !appleSubmitting}
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
        editable={!googleSubmitting && !appleSubmitting}
        value={password}
        onChangeText={setPassword}
        onFocus={() => setFocusedField("password")}
        onBlur={() => setFocusedField(null)}
      />
      {error ? <Text style={styles.error}>{error}</Text> : null}
      <Pressable
        style={[
          styles.button,
          (!isLoaded || anySubmitting) && styles.buttonDisabled,
        ]}
        onPress={onSignIn}
        disabled={!isLoaded || anySubmitting}
      >
        {submitting ? (
          <ActivityIndicator color={colors.white} />
        ) : (
          <Text style={styles.buttonText}>Sign in</Text>
        )}
      </Pressable>
      <Text style={styles.hint}>New here? Create your account at thelightbulb.company</Text>
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
    textAlign: "center",
    marginBottom: spacing.xl,
  },
  // Apple's HIG: their button must have equal-or-greater prominence than
  // other third-party sign-in buttons — black fill, listed first.
  // AppleAuthenticationButton is a NATIVE view with no intrinsic size, so an
  // explicit height is required or it lays out at zero and looks missing. 46
  // matches the Google button below (spacing.md × 2 + a 22 line-height).
  appleButton: {
    height: 46,
    width: "100%",
  },
  // Busy stand-in shown while the native sheet is up — same box, Apple's black.
  // The one colour literal outside theme.ts, deliberately: Sign in with Apple
  // is a vendor mark Apple specifies in absolute terms, not a brand token.
  appleButtonBusy: {
    justifyContent: "center",
    alignItems: "center",
    backgroundColor: "#000000",
    borderRadius: radii.lg,
  },
  googleButton: {
    flexDirection: "row",
    justifyContent: "center",
    alignItems: "center",
    gap: spacing.sm,
    backgroundColor: colors.white,
    borderWidth: 1,
    borderColor: colors.border,
    borderRadius: radii.lg,
    paddingVertical: spacing.md,
  },
  // Inline "G" mark: a serif glyph in the accent color — no image asset.
  googleMark: {
    fontFamily: fonts.loraMedium,
    fontSize: 17,
    lineHeight: 22,
    color: colors.forest,
  },
  googleButtonText: {
    color: colors.ink,
    fontFamily: fonts.sansMedium,
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
    backgroundColor: colors.creamDeeper,
    borderWidth: 1,
    borderColor: colors.border,
    borderRadius: radii.lg,
    paddingHorizontal: spacing.md,
    paddingVertical: spacing.md,
    fontFamily: fonts.sans,
    fontSize: 15,
    color: colors.ink,
  },
  inputFocused: { borderColor: colors.forest },
  button: {
    backgroundColor: colors.forest,
    borderRadius: radii.lg,
    paddingVertical: spacing.md,
    alignItems: "center",
    marginTop: spacing.xs,
  },
  buttonDisabled: { opacity: 0.5 },
  buttonText: {
    color: colors.white,
    fontFamily: fonts.sansMedium,
    fontSize: 15,
    lineHeight: 22,
  },
  error: { ...type.caption, color: colors.error },
  hint: { ...type.caption, textAlign: "center", marginTop: spacing.md },
});
