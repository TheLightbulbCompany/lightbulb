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
  Linking,
  Platform,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
  View,
} from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import { ApiError, createApiClient } from "@lightbulb/shared";

import { colors, fonts, radii, spacing } from "../../config/theme";
import { makeAdapter } from "../../lib/adapter";
import { writeFirstRun } from "../../lib/firstRun";

type Path = "new" | "grow";
/** "noIdea" = they tapped "I don't know yet — you pick": submit with no input. */
type Mode = "idea" | "noIdea";

// After this many CONSECUTIVE failed submits carrying the same upstream
// error, the generic "Couldn't save that just yet. Retry" line — which
// implies a transient problem — gives way to a persistent error state that
// shows the upstream detail and points at support. A different error (or a
// success) resets the streak. Mirrors the web funnel's FirstRunSteps.
const PERSISTENT_FAILURE_THRESHOLD = 3;

/** Best-effort upstream detail for a failed submit: FastAPI's body.detail,
 * else the error message, else the HTTP status. Mirrors the web funnel's. */
function extractErrorDetail(err: unknown): string {
  if (err instanceof ApiError) {
    const detail = (err.body as { detail?: unknown } | null)?.detail;
    if (typeof detail === "string" && detail.length > 0) return detail;
    return err.message || `HTTP ${err.status}`;
  }
  if (err instanceof Error && err.message) return err.message;
  return "Unknown error";
}

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
  // Consecutive-failure tracking for the persistent-error switch. One
  // completed submit() that lands in state "error" == one failure; the
  // streak only grows while the upstream detail stays the same.
  const [failure, setFailure] = useState<{ detail: string; streak: number } | null>(null);

  const recordFailure = useCallback((errorDetail: string) => {
    setFailure((prev) =>
      prev && prev.detail === errorDetail
        ? { detail: errorDetail, streak: prev.streak + 1 }
        : { detail: errorDetail, streak: 1 },
    );
  }, []);

  const submit = useCallback(
    async (submitMode: Mode) => {
      const noIdea = submitMode === "noIdea";
      setMode(submitMode);
      if (!user) return;
      if (!noIdea && input.trim().length < 3) return;
      setState("submitting");
      try {
        // No input at all on the "you pick" button: the absent idea IS the
        // signal, and BRIEF.md tells Friday to choose one.
        const resp = await api.post("/onboarding/brief", {
          path,
          input: noIdea ? undefined : input.trim(),
          detail:
            !noIdea && path === "new" && detail.trim() ? detail.trim() : undefined,
        });
        const status = (resp as { status?: string } | null)?.status;
        if (status !== "staged" && status !== "already_submitted") {
          // Never dismiss the funnel over an answer that was never saved.
          recordFailure(
            typeof (resp as { detail?: unknown } | null)?.detail === "string"
              ? (resp as { detail: string }).detail
              : `Unexpected response: ${JSON.stringify(resp) ?? "empty"}`,
          );
          setState("error");
          return;
        }
        setFailure(null);
        // Intake is durable server-side the moment this resolves; the seeding
        // it drives is server-owned. `briefSubmitted` is written here purely
        // as an optimistic pre-poll paint (dismisses the funnel THIS
        // session); it is never read as authority for whether the kickoff
        // ran.
        await writeFirstRun(user, {
          briefSubmitted: true,
          path: path ?? undefined,
        });
      } catch (err) {
        recordFailure(extractErrorDetail(err));
        setState("error");
      }
    },
    [api, user, path, input, detail, recordFailure],
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
            {state === "error" && failure && (
              <View style={styles.errorBlock}>
                {failure.streak >= PERSISTENT_FAILURE_THRESHOLD ? (
                  // Persistent failure: the same upstream error N times in a
                  // row is not transient — stop implying a retry will fix it.
                  // Show the upstream detail and route to support instead.
                  <View style={styles.escalatedBlock}>
                    <Text style={styles.errorText}>We couldn’t save your brief.</Text>
                    <Text style={styles.errorDetail}>{failure.detail}</Text>
                    <Text style={styles.errorText}>
                      Please{" "}
                      <Text
                        style={styles.inlineLink}
                        accessibilityRole="link"
                        onPress={() => void Linking.openURL("mailto:support@isol8.co")}
                      >
                        contact support
                      </Text>{" "}
                      and we’ll get it sorted.
                    </Text>
                  </View>
                ) : (
                  <Text style={styles.errorText}>
                    Couldn’t save that just yet.{" "}
                    <Text style={styles.inlineLink} onPress={() => void submit(mode)}>
                      Retry
                    </Text>
                  </Text>
                )}
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
  // Tighter than errorBlock: the persistent-failure copy is one paragraph in
  // three parts, not three separate affordances (web: space-y-1.5).
  escalatedBlock: { gap: spacing.xs, alignItems: "flex-start" },
  errorText: {
    fontFamily: fonts.dmSans,
    fontSize: 13,
    lineHeight: 18,
    color: colors.danger,
  },
  // The upstream detail is diagnostic, not the message — muted and smaller so
  // it reads as the cause rather than competing with the copy around it.
  errorDetail: {
    fontFamily: fonts.dmSans,
    fontSize: 12,
    lineHeight: 16,
    color: colors.mute,
  },
  inlineLink: { textDecorationLine: "underline", color: colors.danger },
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
