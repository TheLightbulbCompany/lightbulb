/**
 * Provision-gate host (W5 contract §B3, LEAN).
 *
 * Mounts the `useContainerStatus` probe ABOVE the Tabs (in (app)/_layout.tsx —
 * chat.tsx is off-limits, so the banner can't live inside it). Two surfaces:
 *
 *   • Cold start / parked wake — delegated to <ProvisioningStepper/> (mobile
 *     onboarding-parity Slice 3), which owns the time-box: a slim amber bar
 *     over live children for a `parked` wake or a quick `starting`, promoted
 *     to a full-screen phased stepper once a `starting` container has
 *     genuinely dragged. Either way it auto-clears once ready.
 *   • Blocking interstitial — a full-screen card for `teamPending` / `blocked`
 *     / `needsSignup` / `error`, which each mean there is no working chat
 *     behind the surface (contract §B3). `teamPending` (an org member whose
 *     admin hasn't provisioned the workspace yet) shows a pulsing "Setting
 *     up your team workspace…" affordance + Retry — NOT a thin bar over a
 *     broken chat. `blocked` / `needsSignup` show an OUTBOUND "Manage on
 *     isol8.co" link (Linking.openURL) — the mobile app never reimplements
 *     billing / checkout (D1). admin-only gates tell org members to ask
 *     their workspace admin instead of showing a CTA. `error` (mobile
 *     onboarding-parity Slice 1 — the backend-reported container failure;
 *     see `containerStatus.ts`) shows Retry only, no outbound link — there
 *     is nothing to manage on isol8.co for a failed provision, only a
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
 * `loading` / `ready` / `unreachable` never block — the app renders normally.
 * Polling continues while `teamPending` (see useContainerStatus) so the gate
 * clears itself once the admin's workspace comes up.
 */
import { useAuth } from "@clerk/clerk-expo";
import { useCallback } from "react";
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

import { usePulse } from "../chat/usePulse";
import { colors, fonts, radii, spacing } from "../../config/theme";
import {
  useContainerStatus,
  type ContainerStatus,
} from "../../lib/containerStatus";
import { ContainerStatusContext } from "../../lib/containerStatusContext";
import { ByoKeyScreen } from "./ByoKeyScreen";
import { ChatGPTConnectScreen } from "./ChatGPTConnectScreen";
import { ProvisioningStepper } from "./ProvisioningStepper";

const WEB_ORIGIN = "https://isol8.co";
const DEFAULT_MANAGE_URL = `${WEB_ORIGIN}/settings`;

/** Relative gate hrefs (e.g. "/settings/billing") → absolute isol8.co URL. */
function resolveManageUrl(href: string | null): string {
  if (!href) return DEFAULT_MANAGE_URL;
  if (href.startsWith("http")) return href;
  return `${WEB_ORIGIN}${href}`;
}

// ---------------------------------------------------------------------------
// Blocking interstitial
// ---------------------------------------------------------------------------

/** The pulsing "8" glyph — animated for the team-pending "still working" cue. */
function InterstitialGlyph({ pulse }: { pulse: boolean }) {
  const opacity = usePulse(pulse, 0.35);
  if (!pulse) {
    return <Text style={styles.interstitialGlyph}>8</Text>;
  }
  return (
    <Animated.Text style={[styles.interstitialGlyph, { opacity }]}>
      8
    </Animated.Text>
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
  const { signOut } = useAuth();
  // The interstitial replaces the whole tab shell, so it's the only exit for
  // a wrong-account or hard-blocked user — offer sign-out here or they're
  // trapped with just Retry + the outbound link.
  const onSignOut = useCallback(() => {
    Alert.alert("Sign out", "Sign out of this account?", [
      { text: "Cancel", style: "cancel" },
      { text: "Sign out", style: "destructive", onPress: () => void signOut() },
    ]);
  }, [signOut]);
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
          Ask your workspace admin to finish this on isol8.co.
        </Text>
      </View>
    );
  }
  // Keep the "on isol8.co" cue: the button leaves the app (D1 — billing /
  // checkout / OAuth never happen in-app).
  const label = `${actionLabel || "Manage"} on isol8.co`;
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
      "Your workspace needs attention before your agents can run. Manage it on isol8.co.",
  };
}

// ---------------------------------------------------------------------------
// Gate
// ---------------------------------------------------------------------------

export function ProvisionGate({ children }: { children: React.ReactNode }) {
  const { status, refetch } = useContainerStatus();

  const onRetry = useCallback(() => {
    void refetch();
  }, [refetch]);

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
    const copy = blockedCopy(status);
    return (
      <Interstitial
        headline={copy.headline}
        body={copy.body}
        onRetry={onRetry}
        retrying={false}
        action={
          <ManageAction
            adminOnly={status.adminOnly}
            manageUrl={resolveManageUrl(status.href)}
            actionLabel={status.actionLabel}
          />
        }
      />
    );
  }

  if (status.kind === "needsSignup") {
    return (
      <Interstitial
        headline="Finish setting up on isol8.co"
        body="Your workspace hasn't been set up yet. Complete onboarding on the web to start chatting with your agents."
        onRetry={onRetry}
        retrying={false}
        action={<ManageAction adminOnly={false} manageUrl={DEFAULT_MANAGE_URL} />}
      />
    );
  }

  // Container failed (mobile onboarding-parity Slice 1): the backend
  // reports phase="provisioning" for a failed container too, so without
  // this check `status.kind` would read "starting" forever — the gap this
  // slice closes. Unifies with web's ProvisioningStepper "Setup failed"
  // card, which keys on the same raw `container.status === "error"`. A
  // blocking interstitial (there is no working chat behind a failed
  // container) with Retry + sign-out, mirroring the other blocked states —
  // no outbound link, since there's nothing to manage on isol8.co for this
  // one (retrying re-provisioning is the only lever either surface offers).
  if (status.kind === "error") {
    return (
      <Interstitial
        headline="Setup failed"
        body="Something went wrong while setting up your workspace. This is usually temporary — try again in a moment."
        onRetry={onRetry}
        retrying={false}
      />
    );
  }

  // `starting` (cold start) and `parked` (deliberately-asleep trial box) hand
  // off to ProvisioningStepper, which owns the whole time-box (mobile
  // onboarding-parity Slice 3): the thin non-blocking banner over live
  // children for a parked wake or a quick start, promoted to a full-screen
  // phased stepper only once a `starting` container has genuinely dragged.
  // A parked container is healthy and one intent away from running, so it
  // never promotes — it must not blank the chat or spin forever.
  //
  // `children` only ever mounts from this branch on (every earlier return
  // above replaces the whole tree with an interstitial), so this is the one
  // place that needs to hand the polled status down via
  // ContainerStatusContext — FirstRunGate reads it from there instead of
  // running its own second `/container/status`-adjacent poll (mobile
  // onboarding-parity Slice 2).
  if (status.kind === "starting" || status.kind === "parked") {
    return (
      <ContainerStatusContext.Provider value={status}>
        <ProvisioningStepper status={status} onRetry={onRetry}>
          {children}
        </ProvisioningStepper>
      </ContainerStatusContext.Provider>
    );
  }

  return (
    <ContainerStatusContext.Provider value={status}>
      <View style={styles.root}>{children}</View>
    </ContainerStatusContext.Provider>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1 },

  // Blocking interstitial.
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
  interstitialGlyph: {
    fontFamily: fonts.loraItalic,
    fontSize: 48,
    lineHeight: 56,
    color: colors.forest,
  },
  // Lora, not type.title's DM Sans Bold — matches web's `.provision-title`
  // (Lora 400) so the onboarding surfaces read the same on both clients.
  interstitialHeadline: {
    fontFamily: fonts.lora,
    fontSize: 20,
    lineHeight: 26,
    color: colors.ink,
    textAlign: "center",
  },
  interstitialBody: {
    fontFamily: fonts.dmSans,
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
    fontFamily: fonts.dmSans,
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
    fontFamily: fonts.dmSansMedium,
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
    fontFamily: fonts.dmSansMedium,
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
    fontFamily: fonts.dmSans,
    fontSize: 13,
    lineHeight: 17,
    color: colors.mute,
  },
  pressed: { opacity: 0.6 },
});
