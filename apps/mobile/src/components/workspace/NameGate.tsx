/**
 * A signed-in person Clerk has no name for is asked for one before anything else.
 *
 * Sign in with Apple + "Hide My Email" (or an email-code sign-up) gives us no name,
 * and every room, roster and notification then has nothing real to call them. Sits
 * above WorkspaceGate in (app)/_layout.tsx, so the name exists before a workspace
 * is joined or opened. Most Apple users never see it: sign-in keeps Apple's own
 * `fullName` (lib/appleSignIn.ts).
 */
import { useAuth, useUser } from "@clerk/clerk-expo";
import { useState, type ReactNode } from "react";
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

export function NameGate({ children }: { children: ReactNode }) {
  const { isLoaded, user } = useUser();
  const { signOut } = useAuth();
  const [first, setFirst] = useState("");
  const [last, setLast] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!isLoaded || !user) return null;
  if (user.firstName?.trim() || user.username) return <>{children}</>;

  const ready = !!first.trim() && !saving;
  async function onSave() {
    if (!user || !ready) return;
    setSaving(true);
    setError(null);
    try {
      // Clerk refreshes `user`; the next render falls through to the app.
      await user.update({ firstName: first.trim(), lastName: last.trim() });
    } catch {
      setError("We couldn't save your name. Try again.");
    } finally {
      setSaving(false);
    }
  }

  return (
    <KeyboardAvoidingView
      style={styles.page}
      behavior={Platform.OS === "ios" ? "padding" : undefined}
      testID="name-gate"
    >
      <Text style={styles.title} accessibilityRole="header">
        What should people call you?
      </Text>
      <Text style={styles.lede}>
        Your teammates and agents see this name next to everything you send.
      </Text>
      <TextInput
        style={styles.input}
        placeholder="First name"
        accessibilityLabel="First name"
        placeholderTextColor={colors.muted}
        autoCapitalize="words"
        autoComplete="given-name"
        textContentType="givenName"
        autoFocus
        maxLength={64}
        editable={!saving}
        value={first}
        onChangeText={setFirst}
        returnKeyType="next"
      />
      <TextInput
        style={styles.input}
        placeholder="Last name (optional)"
        accessibilityLabel="Last name (optional)"
        placeholderTextColor={colors.muted}
        autoCapitalize="words"
        autoComplete="family-name"
        textContentType="familyName"
        maxLength={64}
        editable={!saving}
        value={last}
        onChangeText={setLast}
        returnKeyType="done"
        onSubmitEditing={() => void onSave()}
      />
      {error ? <Text style={styles.error}>{error}</Text> : null}
      <Pressable
        style={[styles.primary, !ready && styles.disabled]}
        onPress={() => void onSave()}
        disabled={!ready}
        accessibilityRole="button"
        accessibilityState={{ disabled: !ready }}
      >
        {saving ? (
          <ActivityIndicator color={colors.white} />
        ) : (
          <Text style={styles.primaryText}>Continue</Text>
        )}
      </Pressable>
      <Pressable
        style={styles.secondary}
        onPress={() => void signOut()}
        disabled={saving}
        accessibilityRole="button"
      >
        <Text style={styles.secondaryText}>Sign out</Text>
      </Pressable>
    </KeyboardAvoidingView>
  );
}

const styles = StyleSheet.create({
  page: {
    flex: 1,
    justifyContent: "center",
    padding: spacing.xl,
    gap: spacing.md,
    backgroundColor: colors.cream,
  },
  title: { ...type.display, textAlign: "center" },
  lede: { ...type.body, color: colors.inkSoft, textAlign: "center", marginBottom: spacing.md },
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
  primary: {
    backgroundColor: colors.forest,
    borderRadius: radii.lg,
    paddingVertical: spacing.md,
    alignItems: "center",
    marginTop: spacing.xs,
  },
  disabled: { opacity: 0.5 },
  primaryText: { color: colors.white, fontFamily: fonts.sansMedium, fontSize: 15, lineHeight: 22 },
  secondary: { alignItems: "center", paddingVertical: spacing.sm },
  secondaryText: { ...type.caption },
  error: { ...type.caption, color: colors.danger },
});
