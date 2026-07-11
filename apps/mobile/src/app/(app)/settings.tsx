import { useAuth, useOrganization, useUser } from "@clerk/clerk-expo";
import { useFocusEffect } from "expo-router";
import { useCallback, useMemo, useRef, useState, type ReactNode } from "react";
import {
  Alert,
  Animated,
  Linking,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  View,
} from "react-native";
import { ApiError, createApiClient } from "@isol8/shared";

import { usePulse } from "../../components/chat/usePulse";
import { colors, fonts, radii, spacing, type } from "../../config/theme";
import { makeAdapter } from "../../lib/adapter";
import { unregisterDevice } from "../../lib/pushDevice";

// Web-managed billing surface (W2 contract §5): mobile never calls
// POST /billing/portal — it deep-links out to the web settings page instead.
const MANAGE_BILLING_URL = "https://isol8.co/settings?panel=billing";

// ---------------------------------------------------------------------------
// Contract shapes (W2 billing contract §2)
// ---------------------------------------------------------------------------

interface BillingAccount {
  is_subscribed: boolean;
  current_spend: number;
  lifetime_spend: number;
  subscription_status: string | null;
  /** Unix epoch SECONDS. Stripe owns the trial clock — render-only. */
  trial_end: number | null;
  provider_choice: "chatgpt_oauth" | "byo_key" | "bedrock_claude" | null;
  byo_provider: "openai" | "anthropic" | null;
}

interface CreditsBalance {
  balance_microcents: number | null;
  /** Pre-formatted "%.2f" without "$", null when balance unavailable. */
  balance_dollars: string | null;
  lifecycle_enabled?: boolean;
}

interface ChatGPTStatus {
  connected: boolean;
  account_id: string | null;
}

type BillingState =
  | { kind: "loading" }
  | { kind: "error"; message: string }
  /** 404 from /billing/account — pre-subscribe, not an error (contract §1). */
  | { kind: "none" }
  /** 403 — billing managed by the org admin (contract §3 org gating). */
  | { kind: "restricted" }
  | {
      kind: "ready";
      account: BillingAccount;
      credits: CreditsBalance | null;
      chatgpt: ChatGPTStatus | null;
    };

// ---------------------------------------------------------------------------
// Data hook — refetches on every screen focus (useFocusEffect), so a plan
// change made on the web is reflected next time the tab is opened.
// ---------------------------------------------------------------------------

function useBillingData(orgRestricted: boolean) {
  const { getToken } = useAuth();
  // clerk-expo hands out a NEW getToken reference on every render. Routing it
  // through a ref (gateway.tsx getTokenRef pattern) keeps the api client and
  // `load` referentially stable, so useFocusEffect fires exactly once per
  // focus instead of looping load → setState → new getToken → reload forever.
  const getTokenRef = useRef(getToken);
  getTokenRef.current = getToken;
  const api = useMemo(
    () => createApiClient(makeAdapter(() => getTokenRef.current())),
    [],
  );
  const orgRestrictedRef = useRef(orgRestricted);
  orgRestrictedRef.current = orgRestricted;
  const [state, setState] = useState<BillingState>({ kind: "loading" });
  // Monotonic load counter: a stale in-flight response from a previous focus
  // never clobbers a newer one.
  const loadSeq = useRef(0);

  const load = useCallback(async () => {
    const seq = loadSeq.current + 1;
    loadSeq.current = seq;
    // Client-side org gate: a non-admin org member would only get a 403 back,
    // so skip the network round-trips entirely (contract §3).
    if (orgRestrictedRef.current) {
      setState({ kind: "restricted" });
      return;
    }
    setState((prev) => (prev.kind === "ready" ? prev : { kind: "loading" }));
    try {
      const account = (await api.get("/billing/account")) as BillingAccount;
      // Secondary probes are best-effort: their failure never fails the card.
      const [credits, chatgpt] = await Promise.all([
        api
          .get("/billing/credits/balance")
          .then((body) => body as CreditsBalance)
          .catch(() => null),
        account.provider_choice === "chatgpt_oauth"
          ? api
              .get("/oauth/chatgpt/status")
              .then((body) => body as ChatGPTStatus)
              .catch(() => null)
          : Promise.resolve(null),
      ]);
      if (loadSeq.current !== seq) return;
      setState({ kind: "ready", account, credits, chatgpt });
    } catch (err) {
      if (loadSeq.current !== seq) return;
      if (err instanceof ApiError && err.status === 404) {
        setState({ kind: "none" });
      } else if (err instanceof ApiError && err.status === 403) {
        setState({ kind: "restricted" });
      } else {
        setState({
          kind: "error",
          message: err instanceof Error ? err.message : "Request failed",
        });
      }
    }
  }, [api]);

  useFocusEffect(
    useCallback(() => {
      void load();
    }, [load]),
  );

  return { state, retry: load };
}

// ---------------------------------------------------------------------------
// Display helpers (contract §3 formatting rules)
// ---------------------------------------------------------------------------

/** Canonical trial-days formula (TrialBanner.tsx): computed render-time. */
function trialDaysLeft(trialEndSec: number): number {
  return Math.max(0, Math.ceil((trialEndSec * 1000 - Date.now()) / 86_400_000));
}

function trialEndLabel(trialEndSec: number): string {
  return new Date(trialEndSec * 1000).toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
  });
}

function statusChipLabel(account: BillingAccount): string | null {
  const status = account.subscription_status;
  // 200 with no Stripe status yet = pre-subscribe account (contract §3).
  if (!status) return "No active subscription";
  if (status === "trialing") {
    if (account.trial_end == null) return "Trial";
    const days = trialDaysLeft(account.trial_end);
    return `Trial · ${days} ${days === 1 ? "day" : "days"} left`;
  }
  if (status === "active") return "Active";
  return status; // render verbatim (chip style uppercases it)
}

/** Provider label mapping (contract §3, LLMPanel labels). */
function providerLabel(
  account: BillingAccount,
  chatgpt: ChatGPTStatus | null,
): { title: string; detail: string | null } {
  switch (account.provider_choice) {
    case "chatgpt_oauth": {
      let detail = "Inference via your ChatGPT account";
      if (chatgpt != null) {
        detail += chatgpt.connected ? " · Connected" : " · Not connected";
      }
      return { title: "Sign in with ChatGPT", detail };
    }
    case "bedrock_claude":
      return { title: "Anthropic Claude via AWS Bedrock", detail: null };
    case "byo_key":
      if (account.byo_provider === "openai") {
        return { title: "Bring your own key", detail: "OpenAI API key" };
      }
      if (account.byo_provider === "anthropic") {
        return { title: "Bring your own key", detail: "Anthropic API key" };
      }
      return {
        title: "Bring your own key",
        detail: "Pick a provider to finish setting up",
      };
    default:
      return {
        title: "No provider selected",
        detail: "You haven't picked a provider yet.",
      };
  }
}

function initialsOf(name: string | null | undefined, email: string): string {
  const trimmedName = (name ?? "").trim();
  if (!trimmedName) {
    // Name-less user: fall back to the email's first two characters.
    const trimmedEmail = email.trim();
    return trimmedEmail ? trimmedEmail.slice(0, 2).toUpperCase() : "?";
  }
  const parts = trimmedName.split(/\s+/).filter(Boolean);
  const first = parts[0]?.[0] ?? "";
  const second = parts.length > 1 ? (parts[parts.length - 1]?.[0] ?? "") : "";
  return `${first}${second}`.toUpperCase();
}

// ---------------------------------------------------------------------------
// Building blocks
// ---------------------------------------------------------------------------

function Card({ children }: { children: ReactNode }) {
  return <View style={styles.card}>{children}</View>;
}

function SkeletonRow({ delayMs, width }: { delayMs: number; width: `${number}%` }) {
  const pulse = usePulse(true, 0.55, 700, delayMs);
  return <Animated.View style={[styles.skeletonLine, { width, opacity: pulse }]} />;
}

/** Loading placeholder shaped like the plan/credits/provider cards (§7). */
function SkeletonCards() {
  return (
    <>
      <Card>
        <SkeletonRow delayMs={150} width="30%" />
        <SkeletonRow delayMs={300} width="72%" />
        <SkeletonRow delayMs={450} width="55%" />
      </Card>
      <Card>
        <SkeletonRow delayMs={450} width="40%" />
        <SkeletonRow delayMs={600} width="64%" />
      </Card>
    </>
  );
}

// ---------------------------------------------------------------------------
// Screen
// ---------------------------------------------------------------------------

export default function Settings() {
  const { getToken, signOut } = useAuth();
  const getTokenRef = useRef(getToken);
  getTokenRef.current = getToken;
  const api = useMemo(
    () => createApiClient(makeAdapter(() => getTokenRef.current())),
    [],
  );
  const { user } = useUser();
  // Org context is optional: personal accounts render no org line, and if the
  // hook has nothing loaded we simply skip it (contract §3 mirrors web).
  const { organization, membership } = useOrganization();

  // Web gating: org member without org:admin sees the restricted state
  // instead of billing details, matching the 403 the backend would return.
  const orgRestricted =
    organization != null && membership != null && membership.role !== "org:admin";
  const { state, retry } = useBillingData(orgRestricted);

  const email = user?.primaryEmailAddress?.emailAddress ?? "";
  const fullName = user?.fullName ?? null;

  const restricted = orgRestricted || state.kind === "restricted";

  const confirmSignOut = useCallback(() => {
    Alert.alert("Sign out", "Are you sure you want to sign out?", [
      { text: "Cancel", style: "cancel" },
      {
        text: "Sign out",
        style: "destructive",
        // Unregister this device (best-effort, while still authenticated) so the
        // leaving user's replies stop pushing to a phone the next account signs
        // into, then sign out regardless of the cleanup result.
        onPress: () =>
          void unregisterDevice((p) => api.del(p)).finally(() => void signOut()),
      },
    ]);
  }, [signOut, api]);

  const manageBillingRow = (
    <Pressable
      style={({ pressed }) => [styles.linkRow, pressed && styles.linkRowPressed]}
      onPress={() => void Linking.openURL(MANAGE_BILLING_URL)}
    >
      <Text style={styles.linkText}>Manage billing on isol8.co</Text>
    </Pressable>
  );

  let billingCards: ReactNode;
  if (restricted) {
    billingCards = (
      <Card>
        <Text style={styles.overline}>PLAN</Text>
        <Text style={styles.bodyText}>
          Billing is managed by your organization admin
        </Text>
      </Card>
    );
  } else if (state.kind === "loading") {
    billingCards = <SkeletonCards />;
  } else if (state.kind === "error") {
    billingCards = (
      <View style={styles.errorCard}>
        <Text style={styles.errorText}>
          Couldn’t load billing: {state.message}
        </Text>
        <Pressable
          style={({ pressed }) => [
            styles.retryButton,
            pressed && styles.retryButtonPressed,
          ]}
          onPress={() => void retry()}
        >
          <Text style={styles.retryText}>Retry</Text>
        </Pressable>
      </View>
    );
  } else if (state.kind === "none") {
    billingCards = (
      <Card>
        <Text style={styles.overline}>PLAN</Text>
        <Text style={styles.bodyText}>
          No billing account yet — finish setup at isol8.co
        </Text>
        {manageBillingRow}
      </Card>
    );
  } else {
    const { account, credits, chatgpt } = state;
    const chip = statusChipLabel(account);
    const trialing =
      account.subscription_status === "trialing" && account.trial_end != null;
    const showCredits =
      account.provider_choice === "bedrock_claude" ||
      credits?.balance_dollars != null;
    const provider = providerLabel(account, chatgpt);

    billingCards = (
      <>
        <Card>
          <Text style={styles.overline}>PLAN</Text>
          <View style={styles.planRow}>
            <Text style={styles.bodyText}>Isol8 — $50 / month</Text>
            {chip != null && (
              <View style={styles.planBadge}>
                <Text style={styles.planBadgeText}>{chip}</Text>
              </View>
            )}
          </View>
          {trialing && account.trial_end != null && (
            <Text style={styles.captionText}>
              Trial ends {trialEndLabel(account.trial_end)}
            </Text>
          )}
          {manageBillingRow}
        </Card>

        {showCredits && (
          <Card>
            <Text style={styles.overline}>BEDROCK CREDITS</Text>
            <Text style={styles.balanceText}>
              {credits?.balance_dollars != null
                ? `$${credits.balance_dollars}`
                : "—"}
            </Text>
            <Text style={styles.captionText}>
              {credits?.balance_dollars != null
                ? // lifecycle_enabled === false ⇒ top-ups unavailable in this
                  // env; absent/undefined = enabled (contract §2).
                  credits.lifecycle_enabled === false
                  ? "Prepaid balance for Bedrock inference."
                  : "Prepaid balance for Bedrock inference. Top up on isol8.co."
                : "Balance unavailable right now."}
            </Text>
          </Card>
        )}

        <Card>
          <Text style={styles.overline}>MODEL PROVIDER</Text>
          <Text style={styles.bodyText}>{provider.title}</Text>
          {provider.detail != null && (
            <Text style={styles.captionText}>{provider.detail}</Text>
          )}
        </Card>
      </>
    );
  }

  return (
    <ScrollView style={styles.page} contentContainerStyle={styles.content}>
      {/* Profile (§5 footer user-card pattern) */}
      <Card>
        <View style={styles.profileRow}>
          <View style={styles.avatar}>
            <Text style={styles.avatarText}>{initialsOf(fullName, email)}</Text>
          </View>
          <View style={styles.profileText}>
            <Text style={styles.profileName} numberOfLines={1}>
              {fullName || email || "Account"}
            </Text>
            {email !== "" && (
              <Text style={styles.captionText} numberOfLines={1}>
                {email}
              </Text>
            )}
            {organization != null && (
              <Text style={styles.captionText} numberOfLines={1}>
                {organization.name}
              </Text>
            )}
          </View>
        </View>
      </Card>

      {billingCards}

      {/* Sign out — destructive-outline row, confirmed before acting. */}
      <Pressable
        style={({ pressed }) => [
          styles.signOutRow,
          pressed && styles.signOutRowPressed,
        ]}
        onPress={confirmSignOut}
      >
        <Text style={styles.signOutText}>Sign out</Text>
      </Pressable>
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  page: { flex: 1, backgroundColor: colors.cream },
  content: {
    padding: spacing.lg,
    gap: spacing.xl,
    paddingBottom: spacing.xl,
  },

  // Grouped white card (radius lg=12, hairline edge).
  card: {
    backgroundColor: colors.white,
    borderRadius: radii.lg,
    borderWidth: 1,
    borderColor: colors.edge,
    padding: spacing.lg,
    gap: spacing.sm,
  },

  overline: { ...type.overline },
  bodyText: {
    fontFamily: fonts.dmSansMedium,
    fontSize: 13,
    lineHeight: 18,
    color: colors.ink,
  },
  captionText: { ...type.caption },

  // Profile card (§5 footer)
  profileRow: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.md,
  },
  avatar: {
    width: 32,
    height: 32,
    borderRadius: 16,
    backgroundColor: colors.avatarBg,
    alignItems: "center",
    justifyContent: "center",
  },
  avatarText: {
    fontFamily: fonts.dmSansBold,
    fontSize: 12,
    color: colors.avatarText,
  },
  profileText: { flex: 1, gap: 2 },
  profileName: {
    fontFamily: fonts.dmSansMedium,
    fontSize: 13,
    lineHeight: 18,
    color: colors.ink,
  },

  // Plan card
  planRow: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.sm,
    flexWrap: "wrap",
  },
  // Plan badge (§5 footer): 10/600 uppercase, chip bg, mute text, radius 4.
  planBadge: {
    backgroundColor: colors.chip,
    borderRadius: radii.xs,
    paddingHorizontal: spacing.xs,
    paddingVertical: 2,
  },
  planBadgeText: {
    fontFamily: fonts.dmSansBold,
    fontSize: 10,
    lineHeight: 14,
    letterSpacing: 0.4,
    textTransform: "uppercase",
    color: colors.mute,
  },
  linkRow: {
    marginTop: spacing.xs,
    alignSelf: "flex-start",
    borderRadius: radii.xs,
  },
  linkRowPressed: { opacity: 0.6 },
  linkText: {
    fontFamily: fonts.dmSansMedium,
    fontSize: 13,
    lineHeight: 18,
    color: colors.forest,
  },

  // Credits card
  balanceText: {
    fontFamily: fonts.dmSansBold,
    fontSize: 20,
    lineHeight: 26,
    color: colors.ink,
  },

  // Loading skeleton (§7)
  skeletonLine: {
    height: 14,
    borderRadius: radii.xs,
    backgroundColor: colors.skeleton,
  },

  // Error card (§4 trio)
  errorCard: {
    padding: spacing.lg,
    backgroundColor: colors.errorBg,
    borderWidth: 1,
    borderColor: colors.errorBorder,
    borderRadius: radii.md,
    gap: spacing.md,
  },
  errorText: {
    fontFamily: fonts.dmSans,
    fontSize: 14,
    lineHeight: 20,
    color: colors.errorText,
  },
  retryButton: {
    alignSelf: "flex-start",
    borderWidth: 1,
    borderColor: colors.errorBorder,
    borderRadius: radii.sm,
    backgroundColor: colors.white,
    paddingHorizontal: spacing.md,
    paddingVertical: spacing.xs,
  },
  retryButtonPressed: { opacity: 0.6 },
  retryText: {
    fontFamily: fonts.dmSansMedium,
    fontSize: 13,
    lineHeight: 18,
    color: colors.errorText,
  },

  // Sign out — standalone destructive-outline row.
  signOutRow: {
    borderWidth: 1,
    borderColor: colors.edge,
    borderRadius: radii.lg,
    backgroundColor: colors.white,
    paddingVertical: spacing.md,
    alignItems: "center",
  },
  signOutRowPressed: { backgroundColor: colors.creamPressed },
  signOutText: {
    fontFamily: fonts.dmSansMedium,
    fontSize: 13,
    lineHeight: 18,
    color: colors.danger,
  },
});
