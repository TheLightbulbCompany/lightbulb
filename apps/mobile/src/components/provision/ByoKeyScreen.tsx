/**
 * In-app "bring your own API key" entry (mobile onboarding-parity Slice 4).
 *
 * Rendered by ProvisionGate when `blocked.code === "byo_provider_required"`
 * — the owner chose the BYO-key provider path but hasn't picked a
 * sub-provider (OpenAI / Anthropic) yet. Two backend calls, in this order:
 *
 *   1. PUT /settings/keys/{provider} — stores the key encrypted (Secrets
 *      Manager), injected into the container at runtime.
 *   2. POST /billing/provider — writes `provider_choice`/`byo_provider` onto
 *      the BILLING row. That row field, not whether a key exists, is what the
 *      provision gate reads (core/services/provision_gate.py's byo_key
 *      branch), and /billing/provider is its only post-signup writer. Skip
 *      this call and the gate re-fires the moment onConnected() re-polls,
 *      looping the user back onto this screen forever.
 *
 * The order is load-bearing in the other direction too: billing_service's
 * change_provider rejects with 409 `byo_key_missing` unless the key is
 * already saved.
 */
import { useAuth } from "@clerk/expo";
import { useCallback, useMemo, useRef, useState } from "react";
import {
  ActivityIndicator,
  Alert,
  KeyboardAvoidingView,
  Platform,
  Pressable,
  StyleSheet,
  Text,
  TextInput,
  View,
} from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import { ApiError, createApiClient } from "@lightbulb/shared";

import { colors, fonts, radii, spacing } from "../../config/theme";
import { makeAdapter } from "../../lib/adapter";
import { Wordmark } from "./Wordmark";

type Provider = "openai" | "anthropic";

// Verbatim copy from ProvisionGate's ManageAction admin note — a member who
// 403s here lands in the same "you can't finish this yourself" state the
// generic interstitial would have shown, so it should read identically.
const ADMIN_ONLY_MESSAGE = "Ask your workspace admin to finish this on thelightbulb.company.";

/**
 * FastAPI's `detail` is a plain string for most raises but a structured
 * `{code, message}` for billing's ChangeProviderError — and the shared client
 * only lifts the string form onto `ApiError.message` (an object detail leaves
 * it as the useless "API 409"). Dig the human-readable half out of both.
 */
function detailMessage(err: ApiError): string | null {
  const detail = (err.body as { detail?: unknown } | null)?.detail;
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object" && "message" in detail) {
    const message = (detail as { message?: unknown }).message;
    if (typeof message === "string") return message;
  }
  return null;
}

function saveErrorMessage(e: unknown): string {
  if (e instanceof ApiError) {
    // BOTH calls org-admin-gate server-side (settings_keys.set_key and
    // billing.change_provider each require_org_admin), but the gate that
    // routes here reports admin_only=false — so a non-admin member DOES
    // reach this screen and WILL 403. Give them the out-of-band route
    // instead of a raw "Organization admin access required".
    if (e.status === 403) return ADMIN_ONLY_MESSAGE;
    // 409s from change_provider (byo_key_missing / subscription_inactive /
    // use_trial_checkout) carry user-facing copy — show it verbatim.
    const detail = detailMessage(e);
    if (detail) return detail;
  }
  return e instanceof Error ? e.message : "Failed to save key";
}

interface Props {
  /**
   * Fired once the key is saved AND the provider choice is committed to the
   * billing row — never on a failed write, which would just re-trip the same
   * gate.
   */
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
      // ByoKeyScreen is the only place that knows the chosen sub-provider, so
      // it owns this write rather than passing byo_provider up. It MUST be
      // /billing/provider: POST /users/sync still accepts a
      // provider_choice/byo_provider body for old clients but SILENTLY IGNORES
      // both fields server-side (routers/users.py says so), so sending it
      // there wrote nothing, left the gate blocking, and looped this screen.
      // Do not re-add that call.
      await api.post("/billing/provider", {
        provider_choice: "byo_key",
        byo_provider: provider,
      });
      // Strictly after the provider write lands. Firing this on a failed
      // write is what re-polled into the same gate and back onto this screen.
      onConnected();
    } catch (e: unknown) {
      // The key PUT is idempotent and already durable if it was step 1 that
      // succeeded, so leaving Save enabled makes a retry resume from here.
      setError(saveErrorMessage(e));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    // The secure keyboard can otherwise cover the Save button on smaller
    // devices, so lift the vertically centered card above it.
    <KeyboardAvoidingView
      testID="byo-keyboard-avoider"
      style={[
        styles.page,
        { paddingTop: insets.top, paddingBottom: insets.bottom },
      ]}
      behavior={Platform.OS === "ios" ? "padding" : undefined}
    >
      <View style={styles.card}>
        <Wordmark />
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
          // The dimmed `styles.disabled` opacity is a SIGHTED-only signal —
          // without this, VoiceOver announces a live, actionable button that
          // does nothing until a key is typed. (An earlier revision dropped
          // it to stop presses no-opping in tests; the real cause there was
          // un-awaited RNTL v14 `fireEvent` calls, since fixed in
          // ByoKeyScreen.test.tsx, so the trade bought nothing and cost a
          // real a11y regression.)
          //
          // Still NOT Pressable's native `disabled` prop: that also drops the
          // element out of the touch-responder chain, and `submit()` already
          // guards on `canSubmit` itself, so a press that does arrive is a
          // no-op either way.
          accessibilityState={{ disabled: !canSubmit }}
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
    </KeyboardAvoidingView>
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
    fontFamily: fonts.sansMedium,
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
    fontFamily: fonts.sansMedium,
    fontSize: 15,
    lineHeight: 20,
    color: colors.white,
  },
  disabled: { opacity: 0.5 },
  pressed: { opacity: 0.6 },
  errorText: {
    fontFamily: fonts.sans,
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
    fontFamily: fonts.sans,
    fontSize: 13,
    lineHeight: 17,
    color: colors.mute,
  },
});
