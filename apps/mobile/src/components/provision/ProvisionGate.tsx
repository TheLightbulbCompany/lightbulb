/**
 * Provision-gate host (W5 contract §B3, LEAN).
 *
 * Mounts the `useContainerStatus` probe ABOVE the Tabs (in (app)/_layout.tsx —
 * chat.tsx is off-limits, so the banner can't live inside it). Two surfaces:
 *
 *   • Non-blocking banner — a slim amber bar for `starting` (a normal
 *     container cold-start; chat still works underneath and the bar
 *     auto-clears once ready).
 *   • Blocking interstitial — a full-screen card for `teamPending` / `blocked`
 *     / `needsSignup`, which each mean there is no working chat behind the
 *     surface (contract §B3). `teamPending` (an org member whose admin hasn't
 *     provisioned the workspace yet) shows a pulsing "Setting up your team
 *     workspace…" affordance + Retry — NOT a thin bar over a broken chat.
 *     `blocked` / `needsSignup` show an OUTBOUND "Manage on isol8.co" link
 *     (Linking.openURL) — the mobile app never reimplements billing / checkout
 *     (D1). admin-only gates tell org members to ask their workspace admin
 *     instead of showing a CTA.
 *
 *     Two `blocked` codes are the exception to D1's "never reimplement" for
 *     provider SIGN-IN specifically (mobile onboarding-parity Slice 4):
 *     `oauth_required` renders <ChatGPTConnectScreen/> (in-app device-code
 *     flow) and `byo_provider_required` renders <ByoKeyScreen/> (in-app key
 *     entry). Every other blocked code — including `provider_required`
 *     (picking a provider in the first place) and all billing/credits gates —
 *     keeps the outbound Linking.openURL handoff untouched.
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
import { colors, fonts, radii, spacing, type } from "../../config/theme";
import {
  useContainerStatus,
  type ContainerStatus,
} from "../../lib/containerStatus";
import { ByoKeyScreen } from "./ByoKeyScreen";
import { ChatGPTConnectScreen } from "./ChatGPTConnectScreen";

const WEB_ORIGIN = "https://isol8.co";
const DEFAULT_MANAGE_URL = `${WEB_ORIGIN}/settings`;

/** Relative gate hrefs (e.g. "/settings/billing") → absolute isol8.co URL. */
function resolveManageUrl(href: string | null): string {
  if (!href) return DEFAULT_MANAGE_URL;
  if (href.startsWith("http")) return href;
  return `${WEB_ORIGIN}${href}`;
}

// ---------------------------------------------------------------------------
// Non-blocking banner
// ---------------------------------------------------------------------------

function PulsingDot() {
  const opacity = usePulse(true, 0.3);
  return <Animated.View style={[styles.bannerDot, { opacity }]} />;
}

function StartingBanner({ label }: { label: string }) {
  const insets = useSafeAreaInsets();
  return (
    <View style={[styles.banner, { paddingTop: insets.top }]}>
      <View style={styles.bannerRow}>
        <PulsingDot />
        <Text style={styles.bannerText} numberOfLines={1}>
          {label}
        </Text>
      </View>
    </View>
  );
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
  if (status.kind === "blocked" && status.code === "oauth_required") {
    return <ChatGPTConnectScreen onConnected={refetch} />;
  }

  if (status.kind === "blocked" && status.code === "byo_provider_required") {
    return <ByoKeyScreen onConnected={refetch} />;
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

  // `starting` (cold start) and `parked` (deliberately-asleep trial box) both
  // render as a NON-blocking banner over live children — a parked container is
  // healthy and one intent away from running, so it must never blank the chat
  // or spin forever. Foregrounding already fired a wake in parallel and the
  // poll carries wake=1, so both bars auto-clear to `ready`.
  return (
    <View style={styles.root}>
      {status.kind === "starting" && (
        <StartingBanner label="Your workspace is starting…" />
      )}
      {status.kind === "parked" && (
        <StartingBanner label="Waking your agents…" />
      )}
      {children}
    </View>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1 },

  // Non-blocking amber bar (spec §4 disconnected banner tokens).
  banner: {
    backgroundColor: colors.bannerBg,
    borderBottomWidth: StyleSheet.hairlineWidth,
    borderBottomColor: colors.bannerBorder,
  },
  bannerRow: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.sm,
    paddingHorizontal: spacing.md,
    paddingVertical: spacing.sm,
  },
  bannerDot: {
    width: 8,
    height: 8,
    borderRadius: 4,
    backgroundColor: colors.connectingYellow,
  },
  bannerText: {
    flex: 1,
    fontFamily: fonts.dmSansMedium,
    fontSize: 13,
    lineHeight: 18,
    color: colors.bannerText,
  },

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
  interstitialHeadline: {
    ...type.title,
    fontSize: 18,
    lineHeight: 24,
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
