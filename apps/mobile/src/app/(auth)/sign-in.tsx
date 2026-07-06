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

import { colors } from "../../config/theme";

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
        style={styles.input}
        placeholder="Email"
        autoCapitalize="none"
        autoComplete="email"
        keyboardType="email-address"
        value={email}
        onChangeText={setEmail}
      />
      <TextInput
        style={styles.input}
        placeholder="Password"
        secureTextEntry
        autoComplete="current-password"
        value={password}
        onChangeText={setPassword}
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
  container: { flex: 1, justifyContent: "center", padding: 24, gap: 12 },
  logo: { fontSize: 36, fontWeight: "700", textAlign: "center", marginBottom: 24 },
  input: {
    borderWidth: 1,
    borderColor: colors.border,
    borderRadius: 10,
    paddingHorizontal: 14,
    paddingVertical: 12,
    fontSize: 16,
  },
  button: {
    backgroundColor: colors.ink,
    borderRadius: 10,
    paddingVertical: 14,
    alignItems: "center",
    marginTop: 4,
  },
  buttonDisabled: { opacity: 0.5 },
  buttonText: { color: colors.white, fontSize: 16, fontWeight: "600" },
  error: { color: colors.error, fontSize: 13 },
  hint: { textAlign: "center", fontSize: 13, opacity: 0.5, marginTop: 12 },
});
