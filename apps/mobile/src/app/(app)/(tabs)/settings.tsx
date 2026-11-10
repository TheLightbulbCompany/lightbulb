import { useAuth, useOrganization, useUser } from "@clerk/expo";
import { Button, HStack, LabeledContent, Picker, Section, Text, VStack } from "../../../components/ui/swiftui";
import {
  accessibilityLabel,
  background,
  font,
  foregroundStyle,
  frame,
  pickerStyle,
  redacted,
  shapes,
  tag,
} from "@expo/ui/swift-ui/modifiers";
import { router, useFocusEffect } from "expo-router";
import { useCallback, useMemo, useRef, useState, type ReactNode } from "react";
import { Alert, Linking, Platform, useColorScheme } from "react-native";
import { deepLinkToSubscriptions } from "expo-iap";
import { ApiError, createApiClient, type SessionNotifications } from "@lightbulb/shared";

import { GroupedList, NavigationRow } from "../../../components/ui/GroupedList";
import { WEB_ORIGIN } from "../../../config/domain";
import { chatPalette, spacing } from "../../../config/theme";
import { makeAdapter } from "../../../lib/adapter";
import { paywallProductIds } from "../../../lib/appleIap";
import { isAppleManaged } from "../../../lib/manageSubscription";
import { isManagedNoChoiceAccount, MANAGED_NO_CHOICE_LABEL } from "../../../lib/managedProvider";
import { unregisterDevice } from "../../../lib/pushDevice";
import { prefsSaver, readSessionNotifications } from "../../../lib/sessionNotifications";

// Billing here never points outside the app. This screen used to carry
// "Upgrade on thelightbulb.company" and "Manage billing on thelightbulb.company" rows out to
// the web checkout; both are gone (see lib/purchaseCta). The one purchase path
// is the in-app Plans row (3.1.1), shown only when the server offers products
// and the workspace isn't Stripe-billed. Plan state, credits, and provider still
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
   * Pricing tier ("base" | "standard" (Workspace) | "ultra_pro" (Ultra)); null
   * for a trial_local row or one predating the plan field. Needed to match
   * `provider_choice_from_row`'s (apps/backend/core/services/container_provision.py)
   * managed-gate fallback for a paid row that never picked a provider.
   */
  plan?: string | null;
  /**
   * Which channel took the money — "apple_iap", or absent for Stripe (every
   * pre-IAP row). Decides where Manage subscription sends the customer: the
   * two systems cannot see each other's subscriptions, so the wrong one shows
   * "no subscriptions" and they conclude they already cancelled. See
   * lib/manageSubscription.
   */
  subscription_source?: string | null;
  /** In-app plans the server sells right now; empty/absent while IAP is off. */
  apple_iap_product_ids?: string[] | null;
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
  // Clerk hands out a NEW getToken reference on every render. Routing it
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

/** Matches the web's free-tier window (`FREE_TIER_DAYS`, apps/frontend/src/lib/planLabel.ts). */
const FREE_TIER_DAYS = 30;

/**
 * Whole days left in the free-tier (`trial_local`) window, clamped to
 * FREE_TIER_DAYS like the web's `freeTierDaysLeft` — a client clock a few
 * minutes slow must not round a 30-day trial up to 31.
 */
function freeTierDaysLeft(trialEndSec: number): number {
  return Math.min(FREE_TIER_DAYS, trialDaysLeft(trialEndSec));
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
  // Card-less free tier (never the raw "trial_local" string): mirror the
  // web Billing page's TrialStatusCard title (settings/page.tsx) so the
  // days-left/ended state isn't web-only.
  if (status === "trial_local") {
    if (account.trial_end == null) return "Free trial";
    if (account.trial_end * 1000 < Date.now()) return "Free trial ended";
    const days = freeTierDaysLeft(account.trial_end);
    return `Free trial · ${days} ${days === 1 ? "day" : "days"} left`;
  }
  // Anything else ("past_due", "unpaid", …) reads as words, not an enum.
  const words = status.replace(/_/g, " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/** Provider label mapping (contract §3, LLMPanel labels). */
export function providerLabel(
  account: BillingAccount,
  chatgpt: ChatGPTStatus | null,
): { title: string; detail: string | null } {
  // No stored provider_choice: the managed-DeepSeek gate is a config-path
  // fallback, never a `provider_choice` row value itself. A cardless free
  // trial AND a paid Base/Workspace/Ultra row that never picked a provider
  // both run there — "No provider selected" would wrongly tell either of
  // them to go pick one. Shared with control/llm.tsx (managedProvider.ts)
  // so the two provider cards can't drift apart.
  if (isManagedNoChoiceAccount(account)) {
    return { ...MANAGED_NO_CHOICE_LABEL };
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
// GroupedList). Palette-dependent, so they're built inside Settings() via the
// `m` useMemo below, from the same chatPalette half GroupedList renders with.
// ---------------------------------------------------------------------------

/** iOS's own loading skeleton: the row's shape, greyed, until billing loads. */
const PLACEHOLDER = [redacted("placeholder"), accessibilityLabel("Loading")];

// ---------------------------------------------------------------------------
// Session notifications: the same per-person prefs the web's Settings card edits.
// The phone gets a push only for a kind set to Banner.
// ---------------------------------------------------------------------------

const NOTIFY_KINDS = [
  { key: "waiting", label: "Waiting on you" },
  { key: "finished", label: "Finished" },
] as const;
const NOTIFY_LEVELS = [
  { value: "banner", label: "Banner" },
  { value: "badge", label: "Badge only" },
  { value: "off", label: "Off" },
] as const;

function SessionNotificationsSection({ api, menu }: { api: ReturnType<typeof createApiClient>; menu: ReturnType<typeof pickerStyle>[] }) {
  const [prefs, setPrefs] = useState<SessionNotifications | null>(null);
  const saver = useRef<ReturnType<typeof prefsSaver> | null>(null);
  useFocusEffect(
    useCallback(() => {
      let cancelled = false;
      readSessionNotifications(api)
        .then((value) => {
          if (cancelled) return;
          setPrefs(value);
          saver.current = prefsSaver(api, value, setPrefs, () =>
            Alert.alert("Notifications", "The setting could not be saved."),
          );
        })
        .catch(() => undefined);
      return () => {
        cancelled = true;
      };
    }, [api]),
  );
  if (prefs === null) return null;
  const save = (change: Partial<SessionNotifications>) => void saver.current?.(change);
  const options = (items: readonly { value: string; label: string }[]) =>
    items.map((item) => (
      <Text key={item.value} modifiers={[tag(item.value)]}>
        {item.label}
      </Text>
    ));
  return (
    <Section title="Session notifications">
      {NOTIFY_KINDS.map(({ key, label }) => (
        <Picker key={key} label={label} selection={prefs[key]} modifiers={menu}
          onSelectionChange={(value: string) => save({ [key]: value })}>
          {options(NOTIFY_LEVELS)}
        </Picker>
      ))}
      <Picker label="Banners on the Mac" selection={prefs.onlyUnfocused ? "unfocused" : "always"} modifiers={menu}
        onSelectionChange={(value: string) => save({ onlyUnfocused: value === "unfocused" })}>
        {options([{ value: "always", label: "Always" }, { value: "unfocused", label: "When Lightbulb isn't focused" }])}
      </Picker>
      <Picker label="Sound" selection={prefs.sound ? "on" : "off"} modifiers={menu}
        onSelectionChange={(value: string) => save({ sound: value === "on" })}>
        {options([{ value: "on", label: "On" }, { value: "off", label: "Off" }])}
      </Picker>
    </Section>
  );
}

// ---------------------------------------------------------------------------
// Screen
// ---------------------------------------------------------------------------

export default function Settings() {
  const scheme = useColorScheme() === "dark" ? "dark" : "light";
  const p = chatPalette[scheme];
  const m = useMemo(
    () => ({
      BODY: [foregroundStyle(p.fg)],
      SECONDARY: [foregroundStyle(p.fgMute)],
      CAPTION: [font({ textStyle: "footnote" }), foregroundStyle(p.fgMute)],
      DANGER: [foregroundStyle(p.alert)],
      ERROR: [foregroundStyle(p.alert)],
      PLACEHOLDER,
      MENU: [pickerStyle("menu")],
      AVATAR: [
        font({ textStyle: "subheadline", weight: "semibold" }),
        foregroundStyle(p.fg),
        frame({ width: 40, height: 40 }),
        background(p.selected, shapes.roundedRectangle({ cornerRadius: 12 })),
      ],
    }),
    [p],
  );
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
    if (isAppleManaged(source) && Platform.OS !== "ios") {
      // On Android the subscriptions deep link opens Google Play, which cannot see an Apple
      // subscription: say where to cancel it instead of offering a button that goes nowhere.
      Alert.alert(
        "Cancel your subscription first",
        "Your subscription is billed through an Apple Account. Deleting this account does NOT cancel it. Cancel it in Settings on the iPhone or iPad that bought it first.",
        [
          { text: "Not now", style: "cancel" },
          { text: "Delete anyway", style: "destructive", onPress: () => promptDeleteAccount() },
        ],
      );
      return;
    }
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
        <Text modifiers={m.BODY}>Billing is managed by your organization admin</Text>
      </Section>
    );
  } else if (state.kind === "loading") {
    // Shaped like the plan and provider sections it stands in for (§7).
    billingSections = (
      <>
        <Section title="Plan">
          <Text modifiers={m.PLACEHOLDER}>Current plan and status</Text>
        </Section>
      </>
    );
  } else if (state.kind === "error") {
    billingSections = (
      <Section>
        <Text modifiers={m.ERROR}>Couldn’t load billing: {state.message}</Text>
        <Button label="Retry" onPress={() => void retry()} />
      </Section>
    );
  } else if (state.kind === "none") {
    billingSections = (
      <Section title="Plan">
        <Text modifiers={m.BODY}>No plan on this account yet</Text>
      </Section>
    );
  } else {
    const { account, credits, chatgpt } = state;
    const chip = statusChipLabel(account);
    const isTrialLocal = account.subscription_status === "trial_local";
    const isTrialLocalEnded =
      isTrialLocal && account.trial_end != null && account.trial_end * 1000 < Date.now();
    const trialing =
      account.subscription_status === "trialing" && account.trial_end != null;
    const showCredits =
      account.provider_choice === "openrouter_managed" ||
      credits?.balance_dollars != null;
    const planName = isTrialLocal ? "Lightbulb Free plan" : "Lightbulb";
    const planNote = isTrialLocalEnded
      ? "This workspace is reading-only. Agents start again when an admin picks the plan."
      : isTrialLocal
        ? "Includes a daily free usage allowance."
        : trialing && account.trial_end != null
          ? `Trial ends ${trialEndLabel(account.trial_end)}`
          : null;

    billingSections = (
      <>
        <Section title="Plan" footer={planNote != null ? <Text>{planNote}</Text> : undefined}>
          {chip != null ? (
            <LabeledContent label={<Text modifiers={m.BODY}>{planName}</Text>}>
              <Text modifiers={m.SECONDARY}>{chip}</Text>
            </LabeledContent>
          ) : (
            <Text modifiers={m.BODY}>{planName}</Text>
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
            <LabeledContent label={<Text modifiers={m.BODY}>Balance</Text>}>
              <Text modifiers={m.SECONDARY}>
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
        {account.is_subscribed && isAppleManaged(account.subscription_source) && Platform.OS !== "ios" ? (
          // Android's subscriptions deep link opens Google Play, which cannot manage an Apple subscription.
          <Section title="Subscription">
            <Text modifiers={m.SECONDARY}>
              Billed through an Apple Account. Manage or cancel it on the iPhone or iPad that bought it.
            </Text>
          </Section>
        ) : account.is_subscribed && isAppleManaged(account.subscription_source) ? (
          <Section
            title="Subscription"
            footer={<Text>Billed through your Apple Account. Manage or cancel in iOS Settings.</Text>}
          >
            <Button label="Manage subscription" onPress={() => void onManageSubscription()} />
          </Section>
        ) : account.is_subscribed ? (
          <Section title="Subscription">
            <Text modifiers={m.SECONDARY}>Billed on the web</Text>
          </Section>
        ) : null}

        {/* In-app plans (3.1.1) — never for a Stripe-billed workspace, which
            the App Store can neither see nor change. */}
        {paywallProductIds(account).length > 0 &&
        !(account.is_subscribed && !isAppleManaged(account.subscription_source)) ? (
          <Section>
            <NavigationRow title="Plans" icon="creditcard" onPress={() => router.push("/plans")} />
          </Section>
        ) : null}
      </>
    );
  }

  return (
    <GroupedList colorScheme={scheme}>
      {/* Profile (§5 footer user-card pattern) */}
      <Section>
        <HStack spacing={spacing.md}>
          <Text modifiers={m.AVATAR}>{initialsOf(fullName, email)}</Text>
          <VStack alignment="leading" spacing={2}>
            <Text modifiers={m.BODY}>{fullName || email || "Account"}</Text>
            {email !== "" ? <Text modifiers={m.CAPTION}>{email}</Text> : null}
            {organization != null ? <Text modifiers={m.CAPTION}>{organization.name}</Text> : null}
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

      <Section title="Workspace">
        {/* Everyone sees it: a member gets "ask an admin" there, not a missing door. */}
        <NavigationRow title="Invite people" icon="person.badge.plus" onPress={() => router.push("/invite")} />
        <NavigationRow
          title="Billing and phone settings"
          icon="globe"
          link
          onPress={() => void Linking.openURL(`${WEB_ORIGIN}/voice/settings`)}
        />
      </Section>

      <SessionNotificationsSection api={api} menu={m.MENU} />

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
        <Button label="Sign out" onPress={confirmSignOut} modifiers={m.DANGER} />
      </Section>

      {/* Account deletion — App Store 5.1.1(v). */}
      <Section>
        <Button
          label="Delete account"
          role="destructive"
          onPress={confirmDeleteAccount}
          modifiers={m.DANGER}
        />
      </Section>
    </GroupedList>
  );
}
