import { useAuth, useOrganization, useUser } from "@clerk/clerk-expo";
import { Button, HStack, LabeledContent, Section, Text, VStack } from "@expo/ui/swift-ui";
import {
  accessibilityLabel,
  background,
  font,
  foregroundStyle,
  frame,
  redacted,
  shapes,
} from "@expo/ui/swift-ui/modifiers";
import { router, useFocusEffect } from "expo-router";
import { useCallback, useMemo, useRef, useState, type ReactNode } from "react";
import { Alert, Linking } from "react-native";
import { deepLinkToSubscriptions } from "expo-iap";
import { ApiError, createApiClient } from "@lightbulb/shared";

import { GroupedList, NavigationRow } from "../../components/ui/GroupedList";
import { WEB_ORIGIN } from "../../config/domain";
import { colors, spacing } from "../../config/theme";
import { makeAdapter } from "../../lib/adapter";
import { isAppleManaged } from "../../lib/manageSubscription";
import { unregisterDevice } from "../../lib/pushDevice";

// Billing is READ-ONLY here. This screen used to carry "Upgrade on flickmeon.ai"
// and "Manage billing on flickmeon.ai" rows out to the web checkout; both are
// gone, along with the plan's price, because App Store guideline 3.1.3(f)
// exempts this app only while it contains no call to action for purchase
// outside it (see lib/purchaseCta). Plan state, credits, and provider still
// render — describing what you have is not a CTA. "Manage subscription" is
// itself pure management, never a purchase path, but it only renders for an
// Apple-billed subscriber (opens iOS Settings via expo-iap); a Stripe/web-billed
// subscriber sees neutral status text instead, with nothing to tap.

// ---------------------------------------------------------------------------
// Contract shapes (W2 billing contract §2)
// ---------------------------------------------------------------------------

interface BillingAccount {
  is_subscribed: boolean;
  subscription_status: string | null;
  /** Unix epoch SECONDS. Stripe owns the trial clock — render-only. */
  trial_end: number | null;
  provider_choice: "chatgpt_oauth" | "byo_key" | "openrouter_managed" | null;
  byo_provider: "openai" | "anthropic" | null;
  /**
   * Which channel took the money — "apple_iap", or absent for Stripe (every
   * pre-IAP row). Decides where Manage subscription sends the customer: the
   * two systems cannot see each other's subscriptions, so the wrong one shows
   * "no subscriptions" and they conclude they already cancelled. See
   * lib/manageSubscription.
   */
  subscription_source?: string | null;
}

interface CreditsBalance {
  balance_microcents: number | null;
  /** Pre-formatted "%.2f" without "$", null when balance unavailable. */
  balance_dollars: string | null;
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

export function statusChipLabel(account: BillingAccount): string | null {
  const status = account.subscription_status;
  // 200 with no Stripe status yet = pre-subscribe account (contract §3).
  if (!status) return "No active subscription";
  if (status === "trialing") {
    if (account.trial_end == null) return "Trial";
    const days = trialDaysLeft(account.trial_end);
    return `Trial · ${days} ${days === 1 ? "day" : "days"} left`;
  }
  if (status === "active") return "Active";
  // Card-less free tier: the plan row itself says "Free plan" — no chip
  // (and never the raw "trial_local" string).
  if (status === "trial_local") return null;
  // Anything else ("past_due", "unpaid", …) reads as words, not an enum.
  const words = status.replace(/_/g, " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/** Provider label mapping (contract §3, LLMPanel labels). */
export function providerLabel(
  account: BillingAccount,
  chatgpt: ChatGPTStatus | null,
): { title: string; detail: string | null } {
  // Free tier stores no provider_choice (`bedrock_trial` is a config-path
  // mode) but runs Claude Sonnet via Bedrock — "No provider selected"
  // would wrongly tell a free user to go pick one.
  if (account.subscription_status === "trial_local" && account.provider_choice == null) {
    return { title: "Claude Sonnet", detail: "Included with the free plan" };
  }
  switch (account.provider_choice) {
    case "chatgpt_oauth": {
      let detail = "Inference via your ChatGPT account";
      if (chatgpt != null) {
        detail += chatgpt.connected ? " · Connected" : " · Not connected";
      }
      return { title: "Sign in with ChatGPT", detail };
    }
    case "openrouter_managed":
      return { title: "Lightbulb-managed models", detail: "Inference uses your workspace credits" };
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
// Row text styles (SwiftUI modifiers; the rows draw in the system font — see
// GroupedList)
// ---------------------------------------------------------------------------

const BODY = [foregroundStyle(colors.ink)];
const SECONDARY = [foregroundStyle(colors.mute)];
const CAPTION = [font({ textStyle: "footnote" }), foregroundStyle(colors.mute)];
const DANGER = [foregroundStyle(colors.danger)];
const ERROR = [foregroundStyle(colors.errorText)];
/** iOS's own loading skeleton: the row's shape, greyed, until billing loads. */
const PLACEHOLDER = [redacted("placeholder"), accessibilityLabel("Loading")];
const AVATAR = [
  font({ textStyle: "subheadline", weight: "semibold" }),
  foregroundStyle(colors.avatarText),
  frame({ width: 40, height: 40 }),
  background(colors.avatarBg, shapes.circle()),
];

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

  // Self-serve account deletion (App Store 5.1.1(v)): explain permanence,
  // then require the account email typed back (same guard as the backend),
  // then run the server-side teardown and sign out. A server refusal (e.g.
  // "make someone else an admin first") is shown verbatim.
  // The sole member takes the workspace, and any paid subscription, with them.
  const endsPaidSubscription =
    organization?.membersCount === 1 &&
    state.kind === "ready" &&
    ["active", "trialing"].includes(state.account.subscription_status ?? "");
  const promptDeleteAccount = useCallback(() => {
    Alert.alert(
      "Delete account",
      "This permanently deletes your account, agents, workspace, and data. It cannot be undone." +
        (endsPaidSubscription
          ? "\n\nYour workspace and its subscription end immediately. There's no refund for the current billing period."
          : ""),
      [
        { text: "Cancel", style: "cancel" },
        {
          text: "Continue",
          style: "destructive",
          onPress: () =>
            Alert.prompt(
              "Confirm deletion",
              `Type your account email (${email}) to confirm.`,
              [
                { text: "Cancel", style: "cancel" },
                {
                  text: "Delete forever",
                  style: "destructive",
                  onPress: (typed?: string) =>
                    void (async () => {
                      try {
                        await api.post("/users/me/delete", {
                          confirm_email: (typed ?? "").trim(),
                        });
                        await unregisterDevice((p) => api.del(p)).catch(() => {});
                        await signOut();
                      } catch (err) {
                        const detail =
                          err instanceof ApiError &&
                          typeof (err.body as { detail?: unknown })?.detail === "string"
                            ? ((err.body as { detail: string }).detail)
                            : null;
                        Alert.alert(
                          "Couldn't delete account",
                          detail === "email_mismatch"
                            ? "That email didn't match this account."
                            : detail === "deletion_incomplete"
                              ? "Deletion didn't finish — try again in a moment, or contact support@thelightbulb.company."
                              : (detail ?? "Something went wrong — try again or contact support@thelightbulb.company."),
                        );
                      }
                    })(),
                },
              ],
              "plain-text",
              "",
              "email-address",
            ),
        },
      ],
    );
  }, [api, email, signOut, endsPaidSubscription]);

  const confirmDeleteAccount = useCallback(() => {
    // An Apple subscription OUTLIVES the account. `wipe_account` deletes the
    // Stripe customer, the billing row and the Clerk user — it cannot touch
    // StoreKit, which only the customer can cancel from iOS Settings. Delete
    // without cancelling and Apple keeps charging for a subscription that now
    // entitles nothing, and the appAccountToken binding means the purchase
    // cannot be restored onto a replacement account either. So say so, and put
    // the cancellation one tap away, BEFORE the irreversible step.
    const source = state.kind === "ready" ? state.account.subscription_source : null;
    if (isAppleManaged(source)) {
      Alert.alert(
        "Cancel your subscription first",
        "Your subscription is billed through your Apple Account. Deleting this account does NOT cancel it — Apple will keep charging you, and the purchase can't be moved to a new account. Cancel in iOS Settings first.",
        [
          { text: "Not now", style: "cancel" },
          {
            text: "Open iOS Settings",
            onPress: () => void deepLinkToSubscriptions().catch(() => {}),
          },
          { text: "Delete anyway", style: "destructive", onPress: () => promptDeleteAccount() },
        ],
      );
      return;
    }
    promptDeleteAccount();
  }, [state, promptDeleteAccount]);


  /**
   * Apple Account subscription management (iOS Settings), via expo-iap's
   * deep link. This is the only "Manage subscription" entry point left —
   * a Stripe/web-billed subscriber never sees the button at all (App Store
   * 3.1.3(f): no call to action that leaves the app for billing), so this is
   * never called with anything but an Apple-billed subscription.
   */
  const onManageSubscription = useCallback(async () => {
    await deepLinkToSubscriptions();
  }, []);

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

  let billingSections: ReactNode;
  if (restricted) {
    billingSections = (
      <Section title="Plan">
        <Text modifiers={BODY}>Billing is managed by your organization admin</Text>
      </Section>
    );
  } else if (state.kind === "loading") {
    // Shaped like the plan and provider sections it stands in for (§7).
    billingSections = (
      <>
        <Section title="Plan">
          <Text modifiers={PLACEHOLDER}>Current plan and status</Text>
        </Section>
        <Section title="Model provider">
          <Text modifiers={PLACEHOLDER}>Provider name</Text>
        </Section>
      </>
    );
  } else if (state.kind === "error") {
    billingSections = (
      <Section>
        <Text modifiers={ERROR}>Couldn’t load billing: {state.message}</Text>
        <Button label="Retry" onPress={() => void retry()} />
      </Section>
    );
  } else if (state.kind === "none") {
    billingSections = (
      <Section title="Plan">
        <Text modifiers={BODY}>No plan on this account yet</Text>
      </Section>
    );
  } else {
    const { account, credits, chatgpt } = state;
    const chip = statusChipLabel(account);
    const isTrialLocal = account.subscription_status === "trial_local";
    const trialing =
      account.subscription_status === "trialing" && account.trial_end != null;
    const showCredits =
      account.provider_choice === "openrouter_managed" ||
      credits?.balance_dollars != null;
    const provider = providerLabel(account, chatgpt);
    const planName = isTrialLocal ? "Lightbulb Free plan" : "Lightbulb";
    const planNote = isTrialLocal
      ? "Includes a daily free usage allowance."
      : trialing && account.trial_end != null
        ? `Trial ends ${trialEndLabel(account.trial_end)}`
        : null;

    billingSections = (
      <>
        <Section title="Plan" footer={planNote != null ? <Text>{planNote}</Text> : undefined}>
          {chip != null ? (
            <LabeledContent label={<Text modifiers={BODY}>{planName}</Text>}>
              <Text modifiers={SECONDARY}>{chip}</Text>
            </LabeledContent>
          ) : (
            <Text modifiers={BODY}>{planName}</Text>
          )}
        </Section>

        {showCredits && (
          <Section
            title="Workspace credits"
            footer={
              <Text>
                {credits?.balance_dollars != null
                  ? "Prepaid balance for managed inference."
                  : "Balance unavailable right now."}
              </Text>
            }
          >
            <LabeledContent label={<Text modifiers={BODY}>Balance</Text>}>
              <Text modifiers={SECONDARY}>
                {credits?.balance_dollars != null ? `$${credits.balance_dollars}` : "—"}
              </Text>
            </LabeledContent>
          </Section>
        )}

        {/* Apple requires an app selling auto-renewable subscriptions to give
            subscribers a way to manage them — that button stays, and it's
            management, never a purchase call to action. A Stripe/web-billed
            subscriber gets neutral status text and nothing to tap, since that
            management surface is the web (App Store 3.1.3(f)). */}
        {account.is_subscribed && isAppleManaged(account.subscription_source) ? (
          <Section
            title="Subscription"
            footer={<Text>Billed through your Apple Account. Manage or cancel in iOS Settings.</Text>}
          >
            <Button label="Manage subscription" onPress={() => void onManageSubscription()} />
          </Section>
        ) : account.is_subscribed ? (
          <Section title="Subscription">
            <Text modifiers={SECONDARY}>Billed on the web</Text>
          </Section>
        ) : null}

        <Section title="Model provider">
          <VStack alignment="leading" spacing={2}>
            <Text modifiers={BODY}>{provider.title}</Text>
            {provider.detail != null ? <Text modifiers={CAPTION}>{provider.detail}</Text> : null}
          </VStack>
        </Section>
      </>
    );
  }

  return (
    <GroupedList>
      {/* Profile (§5 footer user-card pattern) */}
      <Section>
        <HStack spacing={spacing.md}>
          <Text modifiers={AVATAR}>{initialsOf(fullName, email)}</Text>
          <VStack alignment="leading" spacing={2}>
            <Text modifiers={BODY}>{fullName || email || "Account"}</Text>
            {email !== "" ? <Text modifiers={CAPTION}>{email}</Text> : null}
            {organization != null ? <Text modifiers={CAPTION}>{organization.name}</Text> : null}
          </VStack>
        </HStack>
        {(user?.organizationMemberships.length ?? 0) > 1 ? (
          <NavigationRow
            title="Switch workspace"
            icon="arrow.left.arrow.right"
            onPress={() => router.push("/workspaces")}
          />
        ) : null}
      </Section>

      {billingSections}

      {/* Legal links — App Store 5.1.1(i) requires the privacy policy to be
          reachable inside the app. */}
      <Section title="Legal">
        <NavigationRow
          title="Privacy Policy"
          icon="hand.raised"
          link
          onPress={() => void Linking.openURL(`${WEB_ORIGIN}/privacy`)}
        />
        <NavigationRow
          title="Terms of Service"
          icon="doc.text"
          link
          onPress={() => void Linking.openURL(`${WEB_ORIGIN}/terms`)}
        />
      </Section>

      <Section title="Help">
        <NavigationRow
          title="Contact support"
          icon="envelope"
          link
          onPress={() =>
            Linking.openURL("mailto:support@thelightbulb.company").catch(() =>
              Alert.alert("Contact support", "Email support@thelightbulb.company."),
            )
          }
        />
      </Section>

      {/* Sign out — confirmed before acting. */}
      <Section>
        <Button label="Sign out" onPress={confirmSignOut} modifiers={DANGER} />
      </Section>

      {/* Account deletion — App Store 5.1.1(v). */}
      <Section>
        <Button
          label="Delete account"
          role="destructive"
          onPress={confirmDeleteAccount}
          modifiers={DANGER}
        />
      </Section>
    </GroupedList>
  );
}
