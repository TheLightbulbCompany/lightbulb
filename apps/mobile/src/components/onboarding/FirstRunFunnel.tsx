/**
 * Two-path onboarding funnel — ported from the web
 * (apps/frontend/src/components/onboarding/FirstRunFunnel.tsx); keep copy
 * and submit semantics in sync.
 *
 * Shown full-screen (FirstRunGate) while the trial container provisions.
 * Submitting RECORDS the answer server-side; the firstRun metadata write
 * flips the gate off. It is the committed onboarding front door — there is
 * no skip, so submitting the brief is the only way past it.
 *
 * One POST, no retry — matching the web: the endpoint is durable intake, and
 * the seeding it drives (goal, project, BRIEF.md, Friday's kickoff) happens
 * server-owned once the workspace is up, retried by the /container/status
 * poll rather than by the client (see core/services/onboarding_brief.py).
 * Client-side analytics are web-only; the backend emits
 * `first_run_brief_submitted` itself.
 */
import { useAuth, useUser } from "@clerk/clerk-expo";
import { useCallback, useMemo, useRef, useState } from "react";
import {
  Alert,
  KeyboardAvoidingView,
  Platform,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
  View,
} from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import { ApiError, createApiClient } from "@isol8/shared";

import { colors, fonts, radii, spacing } from "../../config/theme";
import { makeAdapter } from "../../lib/adapter";
import { writeFirstRun } from "../../lib/firstRun";

type Path = "new" | "grow";
/** "noIdea" = they tapped "I don't know yet — you pick": submit with no input. */
type Mode = "idea" | "noIdea";
// DEPLOY-SKEW SHIM — see the web funnel for the full reasoning. The endpoint
// is durable intake, so there is nothing to wait out; but a build shipped
// before the backend rollout still meets an OLD backend that 409s "not_ready"
// (or 202s with no recognised status) for the first ~30s of a signup. DELETE
// once the durable-intake backend is deployed everywhere.
const SKEW_RETRY_MS = 3000;
const SKEW_RETRIES = 2;

export function FirstRunFunnel() {
  const insets = useSafeAreaInsets();
  const { user } = useUser();
  const { getToken, signOut } = useAuth();
  const getTokenRef = useRef(getToken);
  getTokenRef.current = getToken;
  const api = useMemo(
    () => createApiClient(makeAdapter(() => getTokenRef.current())),
    [],
  );

  const [path, setPath] = useState<Path | null>(null);
  const [input, setInput] = useState("");
  const [detail, setDetail] = useState("");
  const [state, setState] = useState<"idle" | "submitting" | "error">("idle");
  // Which submission the user made, so Retry replays the same one.
  const [mode, setMode] = useState<Mode>("idea");

  const submit = useCallback(
    async (submitMode: Mode) => {
      const noIdea = submitMode === "noIdea";
      setMode(submitMode);
      if (!user) return;
      if (!noIdea && input.trim().length < 3) return;
      setState("submitting");
      try {
        for (let attempt = 0; ; attempt++) {
          let resp: unknown;
          try {
            // No input at all on the "you pick" button: the absent idea IS the
            // signal, and BRIEF.md tells Friday to choose one.
            resp = await api.post("/onboarding/brief", {
              path,
              input: noIdea ? undefined : input.trim(),
              detail:
                !noIdea && path === "new" && detail.trim() ? detail.trim() : undefined,
            });
          } catch (err) {
            const httpStatus = err instanceof ApiError ? err.status : undefined;
            if (httpStatus === 409 && attempt < SKEW_RETRIES) {
              await new Promise((r) => setTimeout(r, SKEW_RETRY_MS));
              continue;
            }
            throw err;
          }
          const status = (resp as { status?: string } | null)?.status;
          if (status === "staged" || status === "already_submitted") break;
          if (attempt < SKEW_RETRIES) {
            await new Promise((r) => setTimeout(r, SKEW_RETRY_MS));
            continue;
          }
          // Never dismiss the funnel over an answer that was never saved.
          setState("error");
          return;
        }
        // Intake is durable server-side the moment this resolves; the seeding
        // it drives is server-owned. `briefSubmitted` is written here purely
        // as an optimistic pre-poll paint (dismisses the funnel THIS
        // session); it is never read as authority for whether the kickoff
        // ran.
        await writeFirstRun(user, {
          briefSubmitted: true,
          path: path ?? undefined,
        });
      } catch {
        setState("error");
      }
    },
    [api, user, path, input, detail],
  );

  // The funnel replaces the whole tab shell and has no skip (committed front
  // door), so a persistently-failing brief would otherwise trap the user with
  // only Retry. Offer the same confirm-then-signOut escape every provision
  // screen has (ProvisionGate's Interstitial). NOT a skip — submitting the
  // brief stays the only way forward.
  const onSignOut = useCallback(() => {
    Alert.alert("Sign out", "Sign out of this account?", [
      { text: "Cancel", style: "cancel" },
      { text: "Sign out", style: "destructive", onPress: () => void signOut() },
    ]);
  }, [signOut]);

  const busy = state === "submitting";

  return (
    <KeyboardAvoidingView
      style={[styles.page, { paddingTop: insets.top, paddingBottom: insets.bottom }]}
      behavior={Platform.OS === "ios" ? "padding" : undefined}
    >
      <ScrollView
        contentContainerStyle={styles.scroll}
        keyboardShouldPersistTaps="handled"
      >
        {path === null ? (
          <View style={styles.section}>
            <View style={styles.headerBlock}>
              <Text style={styles.headline}>Let’s get started.</Text>
              <Text style={styles.subtitle}>
                Tell your CEO about your business — she’ll research it and lay out
                your next moves.
              </Text>
            </View>
            <View style={styles.choiceBlock}>
              <Pressable
                style={({ pressed }) => [styles.choiceBtn, pressed && styles.pressed]}
                onPress={() => setPath("new")}
                accessibilityRole="button"
              >
                <Text style={styles.choiceBtnText}>Start something new</Text>
              </Pressable>
              <Text style={styles.choiceCaption}>Start from scratch</Text>
              <Pressable
                style={({ pressed }) => [
                  styles.choiceBtn,
                  styles.choiceGap,
                  pressed && styles.pressed,
                ]}
                onPress={() => setPath("grow")}
                accessibilityRole="button"
              >
                <Text style={styles.choiceBtnText}>Grow my business</Text>
              </Pressable>
              <Text style={styles.choiceCaption}>Already have a business</Text>
            </View>
          </View>
        ) : (
          <View style={styles.section}>
            <Pressable onPress={() => setPath(null)} hitSlop={8}>
              <Text style={styles.backLink}>← Back</Text>
            </Pressable>
            <Text style={styles.promptHeadline}>
              {path === "new" ? "What’s your idea?" : "What’s your company’s website?"}
            </Text>
            {path === "new" ? (
              <View style={styles.inputBlock}>
                <TextInput
                  style={[styles.input, styles.inputMultiline]}
                  placeholder="e.g. A social media agency for small restaurants"
                  placeholderTextColor={colors.placeholder}
                  value={input}
                  onChangeText={setInput}
                  multiline
                />
                <TextInput
                  style={styles.input}
                  placeholder="Who's it for? (optional)"
                  placeholderTextColor={colors.placeholder}
                  value={detail}
                  onChangeText={setDetail}
                />
              </View>
            ) : (
              <TextInput
                style={styles.input}
                placeholder="yourcompany.com"
                placeholderTextColor={colors.placeholder}
                value={input}
                onChangeText={setInput}
                autoCapitalize="none"
                autoCorrect={false}
                keyboardType="url"
              />
            )}
            <Pressable
              style={({ pressed }) => [
                styles.choiceBtn,
                (busy || input.trim().length < 3) && styles.disabled,
                pressed && styles.pressed,
              ]}
              disabled={busy || input.trim().length < 3}
              onPress={() => void submit("idea")}
              accessibilityRole="button"
            >
              <Text style={styles.choiceBtnText}>
                {busy && mode === "idea"
                  ? "Setting things up…"
                  : "Continue →"}
              </Text>
            </Pressable>
            {path === "new" && (
              <View>
                <Pressable
                  style={({ pressed }) => [
                    styles.secondaryBtn,
                    busy && styles.disabled,
                    pressed && styles.pressed,
                  ]}
                  disabled={busy}
                  onPress={() => void submit("noIdea")}
                  accessibilityRole="button"
                >
                  <Text style={styles.secondaryBtnText}>
                    {busy && mode === "noIdea"
                      ? "Setting things up…"
                      : "I don’t know yet — you pick"}
                  </Text>
                </Pressable>
                <Text style={styles.choiceCaption}>
                  Friday will look you up and choose something
                </Text>
              </View>
            )}
            {state === "error" && (
              <View style={styles.errorBlock}>
                <Text style={styles.errorText}>
                  Couldn’t save that just yet.{" "}
                  <Text style={styles.retryLink} onPress={() => void submit(mode)}>
                    Retry
                  </Text>
                </Text>
                <Pressable
                  style={({ pressed }) => [
                    styles.signOutButton,
                    pressed && styles.pressed,
                  ]}
                  onPress={onSignOut}
                  accessibilityRole="button"
                  accessibilityLabel="Sign out"
                >
                  <Text style={styles.signOutText}>Sign out</Text>
                </Pressable>
              </View>
            )}
          </View>
        )}
      </ScrollView>
    </KeyboardAvoidingView>
  );
}

const styles = StyleSheet.create({
  page: { flex: 1, backgroundColor: colors.cream },
  scroll: {
    flexGrow: 1,
    justifyContent: "center",
    padding: spacing.xl,
  },
  section: { gap: spacing.lg, maxWidth: 400, width: "100%", alignSelf: "center" },
  headerBlock: { gap: spacing.sm, alignItems: "center" },
  headline: {
    fontFamily: fonts.lora,
    fontSize: 28,
    lineHeight: 34,
    color: colors.ink,
    textAlign: "center",
  },
  subtitle: {
    fontFamily: fonts.dmSans,
    fontSize: 14,
    lineHeight: 20,
    color: colors.mute,
    textAlign: "center",
  },
  promptHeadline: {
    fontFamily: fonts.lora,
    fontSize: 22,
    lineHeight: 28,
    color: colors.ink,
  },
  backLink: {
    fontFamily: fonts.dmSans,
    fontSize: 12,
    lineHeight: 16,
    color: colors.mute,
  },
  choiceBlock: { gap: spacing.xs },
  choiceGap: { marginTop: spacing.md },
  // Brand-primary recipe (web CHOICE_BTN): forest fill, cream text, pill.
  choiceBtn: {
    alignItems: "center",
    justifyContent: "center",
    backgroundColor: colors.forest,
    borderRadius: 999,
    paddingVertical: spacing.lg,
  },
  choiceBtnText: {
    fontFamily: fonts.dmSansMedium,
    fontSize: 14,
    lineHeight: 18,
    color: colors.cream,
  },
  choiceCaption: {
    fontFamily: fonts.loraItalic,
    fontSize: 12,
    lineHeight: 16,
    color: colors.mute,
    textAlign: "center",
  },
  // Outline, not filled: the escape hatch must not compete with Continue.
  secondaryBtn: {
    alignItems: "center",
    justifyContent: "center",
    borderWidth: 1,
    borderColor: colors.edge,
    borderRadius: 999,
    paddingVertical: spacing.md,
    marginBottom: spacing.xs,
  },
  secondaryBtnText: {
    fontFamily: fonts.dmSansMedium,
    fontSize: 14,
    lineHeight: 18,
    color: colors.ink,
  },
  inputBlock: { gap: spacing.sm },
  input: {
    borderWidth: 1,
    borderColor: colors.edge,
    borderRadius: radii.lg,
    backgroundColor: colors.white,
    padding: spacing.md,
    fontFamily: fonts.dmSans,
    fontSize: 14,
    lineHeight: 20,
    color: colors.ink,
  },
  inputMultiline: { minHeight: 112, textAlignVertical: "top" },
  errorBlock: { gap: spacing.sm, alignItems: "flex-start" },
  errorText: {
    fontFamily: fonts.dmSans,
    fontSize: 13,
    lineHeight: 18,
    color: colors.danger,
  },
  retryLink: { textDecorationLine: "underline", color: colors.danger },
  // Matches ProvisionGate's Interstitial signOut recipe; the negative left
  // margin flushes the label under the error text while keeping the padded
  // tap target.
  signOutButton: {
    paddingHorizontal: spacing.lg,
    paddingVertical: spacing.xs,
    marginLeft: -spacing.lg,
  },
  signOutText: {
    fontFamily: fonts.dmSans,
    fontSize: 13,
    lineHeight: 17,
    color: colors.mute,
  },
  disabled: { opacity: 0.5 },
  pressed: { opacity: 0.85 },
});
