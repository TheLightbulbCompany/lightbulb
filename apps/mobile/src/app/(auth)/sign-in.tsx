import { useSignIn } from "@clerk/clerk-expo";
import { useState } from "react";
import {
  ActivityIndicator,
  KeyboardAvoidingView,
  Platform,
  Pressable,
  StyleSheet,
  Text,
  TextInput,
} from "react-native";

import { colors, fonts, radii, spacing, type } from "../../config/theme";

/**
 * Email + password sign-in via Clerk's headless flow. Deliberately minimal
 * for Phase 1: accounts that require another first factor (email code,
 * OAuth) are directed to the web app until those strategies land here.
 */
export default function SignIn() {
  const { signIn, setActive, isLoaded } = useSignIn();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  // Presentational only: which input shows the forest focus border.
  const [focusedField, setFocusedField] = useState<"email" | "password" | null>(null);

  async function onSignIn() {
    if (!isLoaded || submitting) return;
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
      <TextInput
        style={[styles.input, focusedField === "email" && styles.inputFocused]}
        placeholder="Email"
        placeholderTextColor={colors.muted}
        autoCapitalize="none"
        autoComplete="email"
        keyboardType="email-address"
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
        value={password}
        onChangeText={setPassword}
        onFocus={() => setFocusedField("password")}
        onBlur={() => setFocusedField(null)}
      />
      {error ? <Text style={styles.error}>{error}</Text> : null}
      <Pressable
        style={[styles.button, (!isLoaded || submitting) && styles.buttonDisabled]}
        onPress={onSignIn}
        disabled={!isLoaded || submitting}
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
