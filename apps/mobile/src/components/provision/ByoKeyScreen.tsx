/**
 * In-app "bring your own API key" entry (mobile onboarding-parity Slice 4).
 *
 * Rendered by ProvisionGate when `blocked.code === "byo_provider_required"`
 * — the owner chose the BYO-key provider path but hasn't picked a
 * sub-provider (OpenAI / Anthropic) yet. Mirrors the web ByoKeyStep: PUT the
 * key first (stored encrypted, injected into the container at runtime), then
 * POST /users/sync to persist `byo_provider` — that field, not whether a key
 * file exists, is what the provision gate checks (see
 * core/services/provision_gate.py's byo_key branch).
 */
import { useAuth } from "@clerk/clerk-expo";
import { useCallback, useMemo, useRef, useState } from "react";
import {
  ActivityIndicator,
  Alert,
  Pressable,
  StyleSheet,
  Text,
  TextInput,
  View,
} from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import { createApiClient } from "@isol8/shared";

import { colors, fonts, radii, spacing, type } from "../../config/theme";
import { makeAdapter } from "../../lib/adapter";

type Provider = "openai" | "anthropic";

interface Props {
  /** Fired once the key is saved and the provider choice is synced. */
  onConnected: () => void;
}

export function ByoKeyScreen({ onConnected }: Props) {
  const insets = useSafeAreaInsets();
  const { getToken, signOut } = useAuth();
  const getTokenRef = useRef(getToken);
  getTokenRef.current = getToken;
  const api = useMemo(
    () => createApiClient(makeAdapter(() => getTokenRef.current())),
    [],
  );

  const [provider, setProvider] = useState<Provider>("openai");
  const [apiKey, setApiKey] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // This is a full-screen takeover with no other navigation — a wrong-account
  // or otherwise-stuck user needs an escape hatch. Same confirm-then-signOut
  // pattern as ProvisionGate's Interstitial (the every-other-blocked-state
  // screen this one replaces for the byo_provider_required gate).
  const onSignOut = useCallback(() => {
    Alert.alert("Sign out", "Sign out of this account?", [
      { text: "Cancel", style: "cancel" },
      { text: "Sign out", style: "destructive", onPress: () => void signOut() },
    ]);
  }, [signOut]);

  const trimmedKey = apiKey.trim();
  const canSubmit = !submitting && trimmedKey.length > 0;

  const submit = async () => {
    if (!canSubmit) return;
    setSubmitting(true);
    setError(null);
    try {
      // Backend route: PUT /settings/keys/{tool_id} with {api_key} (see
      // routers/settings_keys.py) — mirrors web's ByoKeyStep exactly.
      await api.put(`/settings/keys/${provider}`, { api_key: trimmedKey });
      // ByoKeyScreen is the only place that knows the chosen sub-provider,
      // so it owns the /users/sync call rather than passing byo_provider up.
      await api.post("/users/sync", {
        provider_choice: "byo_key",
        byo_provider: provider,
      });
      onConnected();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Failed to save key");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <View style={[styles.page, { paddingTop: insets.top }]}>
      <View style={styles.card}>
        <Text style={styles.headline}>Bring your own API key</Text>
        <Text style={styles.body}>
          Use your own OpenAI or Anthropic account. We never see your key
          after you save it — it&apos;s stored encrypted and injected into
          your container at runtime.
        </Text>

        <View style={styles.providerRow}>
          {(["openai", "anthropic"] as const).map((p) => {
            const active = provider === p;
            const label = p === "openai" ? "OpenAI" : "Anthropic";
            return (
              <Pressable
                key={p}
                style={[styles.providerChip, active && styles.providerChipActive]}
                onPress={() => setProvider(p)}
                accessibilityRole="radio"
                accessibilityState={{ selected: active }}
                accessibilityLabel={label}
              >
                <Text
                  style={[
                    styles.providerChipText,
                    active && styles.providerChipTextActive,
                  ]}
                >
                  {label}
                </Text>
              </Pressable>
            );
          })}
        </View>

        <TextInput
          style={styles.input}
          value={apiKey}
          onChangeText={setApiKey}
          placeholder={provider === "openai" ? "sk-proj-…" : "sk-ant-…"}
          placeholderTextColor={colors.placeholder}
          secureTextEntry
          autoCapitalize="none"
          autoCorrect={false}
          accessibilityLabel="API key"
        />

        {error ? <Text style={styles.errorText}>{error}</Text> : null}

        <Pressable
          style={({ pressed }) => [
            styles.primaryButton,
            !canSubmit && styles.disabled,
            pressed && styles.pressed,
          ]}
          onPress={() => void submit()}
          // Deliberately NOT using Pressable's native `disabled` prop, NOR
          // accessibilityState.disabled: RN's Pressability responder gating
          // (onStartShouldSetResponder) keys off the `disabled` prop, and
          // @testing-library/react-native's fireEvent press-enablement check
          // keys off accessibilityState — either one flips can make
          // fireEvent.press silently no-op even after canSubmit has settled.
          // canSubmit is guarded inside submit() itself instead; the dimmed
          // `styles.disabled` opacity above is the only "disabled" signal.
          // Mirrors ChatGPTConnectScreen's buttons, which never set either
          // prop for the same reason.
          accessibilityRole="button"
          accessibilityLabel="Save key"
        >
          {submitting ? (
            <ActivityIndicator size="small" color={colors.white} />
          ) : (
            <Text style={styles.primaryButtonText}>Save key</Text>
          )}
        </Pressable>

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
    alignItems: "stretch",
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
    fontSize: 13,
    lineHeight: 19,
    color: colors.mute,
    textAlign: "center",
  },
  providerRow: {
    flexDirection: "row",
    gap: spacing.sm,
  },
  providerChip: {
    flex: 1,
    alignItems: "center",
    paddingVertical: spacing.sm,
    borderRadius: radii.md,
    borderWidth: 1,
    borderColor: colors.edge,
    backgroundColor: colors.cream,
  },
  providerChipActive: {
    borderColor: colors.forest,
    backgroundColor: colors.forestTint,
  },
  providerChipText: {
    fontFamily: fonts.dmSansMedium,
    fontSize: 13,
    lineHeight: 18,
    color: colors.inkSoft,
  },
  providerChipTextActive: {
    color: colors.forest,
  },
  input: {
    borderWidth: 1,
    borderColor: colors.edge,
    borderRadius: radii.md,
    paddingHorizontal: spacing.md,
    paddingVertical: spacing.md,
    fontFamily: fonts.mono,
    fontSize: 14,
    color: colors.ink,
    backgroundColor: colors.cream,
  },
  primaryButton: {
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
  disabled: { opacity: 0.5 },
  pressed: { opacity: 0.6 },
  errorText: {
    fontFamily: fonts.dmSans,
    fontSize: 13,
    lineHeight: 18,
    color: colors.errorText,
    textAlign: "center",
  },
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
