/**
 * Provision-gate host (W5 contract §B3, LEAN).
 *
 * Mounts the `useContainerStatus` probe ABOVE the Tabs (in (app)/_layout.tsx —
 * chat.tsx is off-limits, so the banner can't live inside it). Two surfaces:
 *
 *   • Non-blocking (starting / parked / ready / loading / unreachable) — ALL
 *     five delegated to <ProvisioningStepper/> (mobile onboarding-parity
 *     Slice 3), which owns the time-box: a slim amber bar over live children
 *     for a `parked` wake or a quick `starting`, promoted to a full-screen
 *     phased stepper once a `starting` container has genuinely dragged, and a
 *     plain pass-through for the rest. One element type covers all five on
 *     purpose — see the comment on that return for why branching remounts the
 *     entire tab navigator.
 *   • Blocking interstitial — a full-screen card for `teamPending` / `blocked`
 *     / `needsSignup` / `error`, which each mean there is no working chat
 *     behind the surface (contract §B3). `teamPending` (an org member whose
 *     admin hasn't provisioned the workspace yet) shows a pulsing "Setting
 *     up your team workspace…" affordance + Retry — NOT a thin bar over a
 *     broken chat. A NON-purchase `blocked` code shows an OUTBOUND "Manage on
 *     thelightbulb.company" link (Linking.openURL) — the mobile app never reimplements
 *     billing / checkout (D1). admin-only gates tell org members to ask
 *     their workspace admin instead of showing a CTA. Personal subscription
 *     gates hand off to web workspace onboarding before any purchase;
 *     other purchase gates use local state-only copy — except that an org
 *     admin at a SUBSCRIPTION gate gets the in-app StoreKit paywall when the
 *     server offers products (3.1.1), and a member is told to ask the admin. `needsSignup` uses the
 *     same workspace-first web onboarding handoff. `error` (mobile
 *     onboarding-parity Slice 1 — the backend-reported container failure;
 *     see `containerStatus.ts`) shows Retry only, no outbound link — there
 *     is nothing to manage on thelightbulb.company for a failed provision, only a
 *     re-provision to retry.
 *
 *     Two `blocked` codes are the exception to D1's "never reimplement" for
 *     provider SIGN-IN specifically (mobile onboarding-parity Slice 4):
 *     `oauth_required` renders <ChatGPTConnectScreen/> (in-app device-code
 *     flow) and `byo_provider_required` renders <ByoKeyScreen/> (in-app key
 *     entry) — but only when `adminOnly` isn't blocking the CURRENT poller
 *     specifically (checked against `ownerRole`, same admin-only rule as the
 *     generic gate copy below); a non-admin org member still falls through
 *     to "ask your workspace admin" instead of a control they can't use.
 *     Every other blocked code — including `provider_required` (picking a
 *     provider in the first place) and all billing/credits gates — keeps the
 *     outbound Linking.openURL handoff untouched.
 *
 * `loading` / `ready` / `unreachable` never block — the app renders normally
 * (through the same <ProvisioningStepper/> host, which adds nothing for them).
 * Polling continues while `teamPending` (see useContainerStatus) so the gate
 * clears itself once the admin's workspace comes up.
 */
import { useAuth, useOrganization } from "@clerk/expo";
import { createApiClient } from "@lightbulb/shared";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ActivityIndicator,
  Alert,
  Animated,
  Linking,
  Pressable,
  StyleSheet,
  Text,
  View,
} from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";

import { LogoMark } from "../LogoMark";
import { usePulse } from "../chat/usePulse";
import { colors, fonts, radii, spacing } from "../../config/theme";
import {
  useContainerStatus,
  type ContainerStatus,
} from "../../lib/containerStatus";
import { WEB_ORIGIN } from "../../config/domain";
import { makeAdapter } from "../../lib/adapter";
import { paywallProductIds } from "../../lib/appleIap";
import { PaywallScreen } from "../billing/PaywallScreen";
import { ContainerStatusContext } from "../../lib/containerStatusContext";
import { isSubscriptionGate, purchaseGateCopy } from "../../lib/purchaseCta";
import { ByoKeyScreen } from "./ByoKeyScreen";
import { ChatGPTConnectScreen } from "./ChatGPTConnectScreen";
import { ProvisioningStepper } from "./ProvisioningStepper";
import { WorkspaceChooser } from "../workspace/WorkspaceGate";

const DEFAULT_MANAGE_URL = `${WEB_ORIGIN}/settings`;

/** Relative gate hrefs (e.g. "/settings/billing") → absolute web URL. */
function resolveManageUrl(href: string | null): string {
  if (!href) return DEFAULT_MANAGE_URL;
  if (href.startsWith("http")) return href;
  return `${WEB_ORIGIN}${href}`;
}

// ---------------------------------------------------------------------------
// Blocking interstitial
// ---------------------------------------------------------------------------

/** The pulsing bulb mark — animated for the team-pending "still working" cue. */
function InterstitialGlyph({ pulse }: { pulse: boolean }) {
  const opacity = usePulse(pulse, 0.35);
  if (!pulse) {
    return <LogoMark size={48} />;
  }
  return (
    <Animated.View style={{ opacity }}>
      <LogoMark size={48} />
    </Animated.View>
  );
}

function Interstitial({
  headline,
  body,
  onRetry,
  retrying,
  action,
  pulseGlyph = false,
}: {
  headline: string;
  body: string;
  onRetry: () => void;
  retrying: boolean;
  /** Action slot: outbound Manage button, admin note, or nothing (pending). */
  action?: React.ReactNode;
  pulseGlyph?: boolean;
}) {
  const insets = useSafeAreaInsets();
  return (
    <View style={[styles.interstitialPage, { paddingTop: insets.top }]}>
      <View style={styles.interstitialCard}>
        <InterstitialGlyph pulse={pulseGlyph} />
        <Text style={styles.interstitialHeadline}>{headline}</Text>
        <Text style={styles.interstitialBody}>{body}</Text>

        {action}

        <Pressable
          style={({ pressed }) => [styles.retryButton, pressed && styles.pressed]}
          onPress={onRetry}
          disabled={retrying}
          accessibilityRole="button"
          accessibilityLabel="Retry"
        >
          {retrying ? (
            <ActivityIndicator size="small" color={colors.forest} />
          ) : (
            <Text style={styles.retryButtonText}>Retry</Text>
          )}
        </Pressable>

        <SignOutButton />
      </View>
    </View>
  );
}

/**
 * A blocking surface replaces the whole tab shell, so it's the only exit for
 * a wrong-account or hard-blocked user — offer sign-out or they're trapped.
 */
function SignOutButton() {
  const { signOut } = useAuth();
  const onSignOut = useCallback(() => {
    Alert.alert("Sign out", "Sign out of this account?", [
      { text: "Cancel", style: "cancel" },
      { text: "Sign out", style: "destructive", onPress: () => void signOut() },
    ]);
  }, [signOut]);
  return (
    <Pressable
      style={({ pressed }) => [styles.signOutButton, pressed && styles.pressed]}
      onPress={onSignOut}
      accessibilityRole="button"
      accessibilityLabel="Sign out"
    >
      <Text style={styles.signOutText}>Sign out</Text>
    </Pressable>
  );
}

type BillingAccount = {
  apple_iap_product_ids?: string[] | null;
  base_price_usd?: number;
  workspace_price_usd?: number;
  ultra_price_usd?: number;
};

/**
 * The in-app plans the server sells (/billing/account), read only while
 * `enabled` — i.e. while an org workspace sits at a subscription gate. Empty
 * while loading, on failure, and whenever the server has IAP switched off.
 */
function useOfferedProducts(
  enabled: boolean,
  orgId: string | undefined,
  retryTick: number,
) {
  const { getToken } = useAuth();
  const getTokenRef = useRef(getToken);
  getTokenRef.current = getToken;
  const api = useMemo(
    () => createApiClient(makeAdapter(() => getTokenRef.current())),
    [],
  );
  const [account, setAccount] = useState<BillingAccount | null>(null);
  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    api
      .get("/billing/account")
      .then((body) => {
        if (!cancelled) setAccount(body as BillingAccount);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [api, enabled, orgId, retryTick]);
  return { offeredProducts: paywallProductIds(account), account, api };
}

/** Outbound gate CTA / admin-only note for the gate states. */
function ManageAction({
  adminOnly,
  manageUrl,
  actionLabel,
}: {
  adminOnly: boolean;
  manageUrl: string;
  /** Gate's own verb ("Upgrade", "Set up billing") — falls back to "Manage". */
  actionLabel?: string | null;
}) {
  if (adminOnly) {
    return (
      <View style={styles.adminNote}>
        <Text style={styles.adminNoteText}>
          Ask your workspace admin to finish this on thelightbulb.company.
        </Text>
      </View>
    );
  }
  // Keep the "on thelightbulb.company" cue: the button leaves the app (D1 — billing /
  // checkout / OAuth never happen in-app).
  const label = `${actionLabel || "Manage"} on thelightbulb.company`;
  return (
    <Pressable
      style={({ pressed }) => [styles.primaryButton, pressed && styles.pressed]}
      onPress={() => void Linking.openURL(manageUrl)}
      accessibilityRole="button"
      accessibilityLabel={label}
    >
      <Text style={styles.primaryButtonText}>{label}</Text>
    </Pressable>
  );
}

// ---------------------------------------------------------------------------
// Copy for the blocking states
// ---------------------------------------------------------------------------

function blockedCopy(status: Extract<ContainerStatus, { kind: "blocked" }>): {
  headline: string;
  body: string;
} {
  return {
    headline: status.title,
    body:
      status.message ??
      "Your workspace needs attention before your agents can run. Manage it on thelightbulb.company.",
  };
}

// ---------------------------------------------------------------------------
// Non-blocking Teams-degraded affordance
// ---------------------------------------------------------------------------

/**
 * Teams (Paperclip) seeding failed while the container itself is healthy and
 * chat works (`teams_status: "failed"` on a `ready` body — PR3 honest
 * contract). NON-blocking by contract: a thin banner OVER the live children,
 * never an interstitial that replaces chat. Rendered as a FIXED-index sibling
 * above <ProvisioningStepper/> (same static two-child-array rule the stepper
 * documents for its own banner) so toggling it never remounts the tab
 * navigator underneath. Retry re-probes /container/status — the only lever the
 * app has (D1: the mobile app never reimplements billing/seeding flows).
 */
function TeamsDegradedBanner({ onRetry }: { onRetry: () => void }) {
  const insets = useSafeAreaInsets();
  return (
    <View style={[styles.teamsBanner, { paddingTop: insets.top }]}>
      <View style={styles.teamsBannerRow}>
        <Text style={styles.teamsBannerText} numberOfLines={2}>
          Teams setup didn’t finish. Your agents still work — retry to finish
          setting up your team workspace.
        </Text>
        <Pressable
          onPress={onRetry}
          hitSlop={8}
          accessibilityRole="button"
          accessibilityLabel="Retry Teams setup"
          style={({ pressed }) => [pressed && styles.pressed]}
        >
          <Text style={styles.teamsBannerAction}>Retry</Text>
        </Pressable>
      </View>
    </View>
  );
}

// ---------------------------------------------------------------------------
// Gate
// ---------------------------------------------------------------------------

export function ProvisionGate({ children }: { children: React.ReactNode }) {
  const { status, refetch } = useContainerStatus();
  // Org context means the container owner is an org_ id and must keep the
  // admin-aware settings handoff rather than the personal checkout screen.
  //
  // `isLoaded` is load-bearing, not defensive noise. While Clerk is still
  // resolving, `organization` is null — indistinguishable from "personal" — and
  // /container/status can resolve a purchase gate first. An org admin would
  // then see the personal checkout screen for that window.
  // Unknown owner type must therefore read as "not personal": fail closed.
  const { organization, isLoaded: orgLoaded } = useOrganization();
  const ownerTypeKnown = orgLoaded;
  const isOrgOwner = !ownerTypeKnown || organization != null;

  // Retry re-reads the offered products too, not just the container status.
  const [retryTick, setRetryTick] = useState(0);
  const onRetry = useCallback(() => {
    setRetryTick((t) => t + 1);
    void refetch();
  }, [refetch]);

  const atOrgSubscriptionGate =
    status.kind === "blocked" && isSubscriptionGate(status.code) && isOrgOwner;
  const { offeredProducts, account, api } = useOfferedProducts(
    atOrgSubscriptionGate,
    organization?.id,
    retryTick,
  );
  const insets = useSafeAreaInsets();

  // 409 `workspace_required`: the token carries no org. Same chooser as WorkspaceGate,
  // without auto-activating a lone membership (the client already thinks it has one).
  if (status.kind === "workspaceRequired") return <WorkspaceChooser autoActivate={false} />;

  // Team-pending (202): an org member whose admin hasn't provisioned the shared
  // workspace yet — there is NO working chat behind this, so it BLOCKS the Tabs
  // (contract §B3) rather than sitting as a thin bar over a broken chat. Pulsing
  // affordance + Retry; polling continues so it clears itself once ready.
  if (status.kind === "teamPending") {
    return (
      <Interstitial
        headline="Setting up your team workspace…"
        body="Your workspace admin is still getting things ready. This clears automatically once your team's agents are up — no action needed."
        onRetry={onRetry}
        retrying={false}
        pulseGlyph
      />
    );
  }

  // In-app provider sign-in (mobile onboarding-parity Slice 4). These two
  // gate codes fire when the owner already picked a provider (ChatGPT OAuth
  // or BYO key) but hasn't finished connecting it — a native screen instead
  // of an outbound link. refetch (not the onRetry wrapper) is passed
  // directly as onConnected so the gate re-probes as soon as the flow
  // completes. Every OTHER blocked code — including provider_required
  // itself (no provider chosen yet) and all billing/credits gates — falls
  // through to the generic outbound handoff below, untouched.
  //
  // adminOnly gate (Slice 4 fix): `oauth_required` is admin_only for an ORG
  // workspace (only the admin who runs the device-code flow can bind a
  // ChatGPT account to the shared container — see provision_gate.py's
  // action_admin_only=owner_id.startswith("org_")); a non-admin member must
  // NOT see a control they can't use. Mirrors web's exact formula
  // (ProvisioningStepper.tsx's `showAction`): show the in-app screen only
  // when NOT (adminOnly && not-the-admin) — personal owner + org admin
  // connect in-app, non-admin member falls through to the generic
  // "ask your workspace admin" interstitial below. Applied uniformly to
  // byo_provider_required too, even though its adminOnly is always false
  // today — so both codes share one gating rule instead of two.
  if (status.kind === "blocked") {
    const canConnectInApp = !(status.adminOnly && status.ownerRole !== "admin");
    if (status.code === "oauth_required" && canConnectInApp) {
      return <ChatGPTConnectScreen onConnected={refetch} />;
    }
    if (status.code === "byo_provider_required" && canConnectInApp) {
      return <ByoKeyScreen onConnected={refetch} />;
    }
  }

  if (status.kind === "blocked") {
    // A personal/no-org owner cannot use the org-only Checkout API. Send them
    // to web onboarding first so they can create or select the workspace that
    // will own billing. Organization workspaces keep the admin-aware settings
    // handoff below. isSubscriptionGate, NOT every
    // purchase gate: the latter includes
    // credits_required and payment_past_due, which fire for owners who ALREADY
    // have an active plan — routing those here sells a second subscription that
    // adds no credits and fixes no card, so the customer is billed twice and
    // sees the identical gate afterwards.
    if (isSubscriptionGate(status.code) && !isOrgOwner) {
      return (
        <Interstitial
          headline="Choose your workspace"
          body="Continue on the web to create or select the workspace your agents belong to."
          onRetry={onRetry}
          retrying={false}
          action={
            <ManageAction
              adminOnly={false}
              manageUrl={`${WEB_ORIGIN}/onboarding`}
              actionLabel="Continue"
            />
          }
        />
      );
    }

    // An org workspace with no plan, and the server selling plans in-app
    // (3.1.1): the admin buys right here; a member is told who can.
    const inAppPlans = atOrgSubscriptionGate && offeredProducts.length > 0;
    // State only for purchase gates that the web Checkout cannot resolve, with
    // LOCAL copy rather than the server's purchase call to action. Every other
    // blocked code keeps the server copy and outbound handoff.
    const local = purchaseGateCopy(status.code);
    const copy = local
      ? {
          headline: local.title,
          body:
            inAppPlans && status.ownerRole !== "admin"
              ? "Ask your workspace admin to choose a plan."
              : local.body,
        }
      : blockedCopy(status);
    const interstitial = (
      <Interstitial
        headline={copy.headline}
        body={copy.body}
        onRetry={onRetry}
        retrying={false}
        action={
          local ? null : (
            <ManageAction
              adminOnly={status.adminOnly}
              manageUrl={resolveManageUrl(status.href)}
              actionLabel={status.actionLabel}
            />
          )
        }
      />
    );

    // The admin buys right here. Until StoreKit has products to show (or if it
    // never does) the paywall renders the card above instead — never a dead end.
    if (inAppPlans && status.ownerRole === "admin") {
      return (
        <View
          style={[
            styles.paywallPage,
            { paddingTop: insets.top, paddingBottom: insets.bottom },
          ]}
        >
          <PaywallScreen
            api={api}
            productIds={offeredProducts}
            currentProductId={null}
            onPurchased={onRetry}
            account={account}
            fallback={interstitial}
            footer={<SignOutButton />}
          />
        </View>
      );
    }
    return interstitial;
  }

  // A user with no workspace cannot buy against the org-only Checkout API.
  // Web onboarding creates/selects that owner before it offers billing.
  if (status.kind === "needsSignup") {
    return (
      <Interstitial
        headline="Workspace not set up"
        body="This account doesn't have a workspace yet, so there are no agents to chat with."
        onRetry={onRetry}
        retrying={false}
        action={
          <ManageAction
            adminOnly={false}
            manageUrl={`${WEB_ORIGIN}/onboarding`}
            actionLabel="Continue"
          />
        }
      />
    );
  }

  // Container failed TERMINALLY (mobile onboarding-parity Slice 1 + PR3): the
  // backend reports phase="provisioning" for a failed container too, so this
  // keys off `status.kind === "error"` (classified from substatus, NEVER
  // phase) — otherwise it would read "starting" forever. Unifies with web's
  // ProvisioningStepper "Setup failed" card. A blocking interstitial (there is
  // no working chat behind a failed container) with Retry + sign-out, and no
  // outbound link — there's nothing to manage on thelightbulb.company for this one. The
  // copy is honest that this is a REAL failure: Retry only re-checks status
  // (recovery needs an admin reprovision), so it does not promise "temporary".
  if (status.kind === "error") {
    return (
      <Interstitial
        headline="Setup failed"
        body="Something went wrong setting up your workspace, and it needs attention. Retry to re-check, or contact support if it keeps failing."
        onRetry={onRetry}
        retrying={false}
      />
    );
  }

  // Every remaining (non-blocking) status — starting, parked, ready, loading,
  // unreachable — renders through ONE unconditional return. ProvisioningStepper
  // owns the whole cold-start time-box (mobile onboarding-parity Slice 3): the
  // thin non-blocking banner over live children for a parked wake or a quick
  // start, promoted to a full-screen phased stepper only once a `starting`
  // container has genuinely dragged. A parked container is healthy and one
  // intent away from running, so it never promotes. For ready/loading/
  // unreachable the component is a plain pass-through.
  //
  // Branching here instead — stepper for starting/parked, a bare <View> for the
  // rest — would change the Provider's child element TYPE on every transition,
  // and React tears down a subtree whose type changed. That subtree is the
  // entire live app — including the tab navigator — so a cold start would
  // remount it twice (loading → starting → ready), discarding composer drafts,
  // loaded message lists, scroll position and per-screen mount fetches each
  // time. One element type for all five states is what keeps `children` alive.
  //
  // `children` only ever mounts from here (every earlier return replaces the
  // whole tree with an interstitial), so this is also the one place that hands
  // the polled status down via ContainerStatusContext to in-app consumers.
  return (
    <ContainerStatusContext.Provider value={status}>
      <View style={styles.gateRoot}>
        {/* Non-blocking Teams-degraded banner over the live children. Fixed
            index (0) above the stepper (1) — see TeamsDegradedBanner — so it
            never remounts the tab navigator. Gated on `ready` only: a
            container that isn't up yet has no working chat to sit this over,
            and `teams_status` is only meaningful once the container is fine. */}
        {status.kind === "ready" && status.teamsStatus === "failed" ? (
          <TeamsDegradedBanner onRetry={onRetry} />
        ) : null}
        <ProvisioningStepper status={status}>
          {children}
        </ProvisioningStepper>
      </View>
    </ContainerStatusContext.Provider>
  );
}

const styles = StyleSheet.create({
  // Blocking interstitial. (The non-blocking path has no styles of its own —
  // ProvisioningStepper owns that whole surface, banner and pass-through both.)
  paywallPage: { flex: 1, backgroundColor: colors.cream },
  interstitialPage: {
    flex: 1,
    backgroundColor: colors.cream,
    alignItems: "center",
    justifyContent: "center",
    padding: spacing.xl,
  },
  interstitialCard: {
    width: "100%",
    maxWidth: 400,
    alignItems: "center",
    gap: spacing.md,
    backgroundColor: colors.white,
    borderRadius: radii.lg,
    borderWidth: 1,
    borderColor: colors.edge,
    padding: spacing.xl,
  },
  // Regular weight, not type.title's bold — matches web's `.provision-title`
  // (Satoshi 400) so the onboarding surfaces read the same on both clients.
  interstitialHeadline: {
    fontFamily: fonts.lora,
    fontSize: 20,
    lineHeight: 26,
    color: colors.ink,
    textAlign: "center",
  },
  interstitialBody: {
    fontFamily: fonts.sans,
    fontSize: 14,
    lineHeight: 20,
    color: colors.mute,
    textAlign: "center",
  },
  adminNote: {
    alignSelf: "stretch",
    backgroundColor: colors.creamDeeper,
    borderRadius: radii.md,
    padding: spacing.md,
  },
  adminNoteText: {
    fontFamily: fonts.sans,
    fontSize: 13,
    lineHeight: 18,
    color: colors.inkSoft,
    textAlign: "center",
  },
  primaryButton: {
    alignSelf: "stretch",
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
  retryButton: {
    alignSelf: "center",
    paddingHorizontal: spacing.lg,
    paddingVertical: spacing.sm,
  },
  retryButtonText: {
    fontFamily: fonts.sansMedium,
    fontSize: 14,
    lineHeight: 18,
    color: colors.forest,
  },
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
  pressed: { opacity: 0.6 },

  // Non-blocking host: children fill, the Teams banner (when present) sits
  // above them at a fixed index so the tab navigator never remounts.
  gateRoot: { flex: 1 },
  // Teams-degraded banner — same amber "needs attention" tokens as the
  // stepper's disconnected/starting banner so the two read as one language.
  teamsBanner: {
    backgroundColor: colors.bannerBg,
    borderBottomWidth: StyleSheet.hairlineWidth,
    borderBottomColor: colors.bannerBorder,
  },
  teamsBannerRow: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.sm,
    paddingHorizontal: spacing.md,
    paddingVertical: spacing.sm,
  },
  teamsBannerText: {
    flex: 1,
    fontFamily: fonts.sans,
    fontSize: 13,
    lineHeight: 18,
    color: colors.bannerText,
  },
  teamsBannerAction: {
    fontFamily: fonts.sansMedium,
    fontSize: 13,
    lineHeight: 18,
    color: colors.forest,
  },
});
