/**
 * Provision-gate host (W5 contract §B3, LEAN).
 *
 * Mounts the `useContainerStatus` probe ABOVE the Tabs (in (app)/_layout.tsx —
 * chat.tsx is off-limits, so the banner can't live inside it). Two surfaces:
 *
 *   • Non-blocking banner — a slim amber bar for `starting` / `teamPending`.
 *     Chat still works underneath; the bar auto-clears once ready.
 *   • Blocking interstitial — a full-screen card for `blocked` / `needsSignup`,
 *     which prevent chat from working at all. Every CTA is an OUTBOUND
 *     "Manage on isol8.co" link (Linking.openURL) — the mobile app never
 *     reimplements billing / checkout / OAuth (D1). admin-only gates tell org
 *     members to ask their workspace admin instead of showing a CTA.
 *
 * `loading` / `ready` / `unreachable` never block — the app renders normally.
 */
import { useCallback } from "react";
import {
  ActivityIndicator,
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

function Interstitial({
  headline,
  body,
  adminOnly,
  manageUrl,
  onRetry,
  retrying,
}: {
  headline: string;
  body: string;
  adminOnly: boolean;
  manageUrl: string;
  onRetry: () => void;
  retrying: boolean;
}) {
  const insets = useSafeAreaInsets();
  return (
    <View style={[styles.interstitialPage, { paddingTop: insets.top }]}>
      <View style={styles.interstitialCard}>
        <Text style={styles.interstitialGlyph}>8</Text>
        <Text style={styles.interstitialHeadline}>{headline}</Text>
        <Text style={styles.interstitialBody}>{body}</Text>

        {adminOnly ? (
          <View style={styles.adminNote}>
            <Text style={styles.adminNoteText}>
              Ask your workspace admin to finish this on isol8.co.
            </Text>
          </View>
        ) : (
          <Pressable
            style={({ pressed }) => [
              styles.primaryButton,
              pressed && styles.pressed,
            ]}
            onPress={() => void Linking.openURL(manageUrl)}
            accessibilityRole="button"
            accessibilityLabel="Manage on isol8.co"
          >
            <Text style={styles.primaryButtonText}>Manage on isol8.co</Text>
          </Pressable>
        )}

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
      </View>
    </View>
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

  if (status.kind === "blocked") {
    const copy = blockedCopy(status);
    return (
      <Interstitial
        headline={copy.headline}
        body={copy.body}
        adminOnly={status.adminOnly}
        manageUrl={resolveManageUrl(status.href)}
        onRetry={onRetry}
        retrying={false}
      />
    );
  }

  if (status.kind === "needsSignup") {
    return (
      <Interstitial
        headline="Finish setting up on isol8.co"
        body="Your workspace hasn't been set up yet. Complete onboarding on the web to start chatting with your agents."
        adminOnly={false}
        manageUrl={DEFAULT_MANAGE_URL}
        onRetry={onRetry}
        retrying={false}
      />
    );
  }

  const bannerLabel =
    status.kind === "teamPending"
      ? "Setting up your team workspace…"
      : status.kind === "starting"
        ? "Your workspace is starting…"
        : null;

  return (
    <View style={styles.root}>
      {bannerLabel != null && <StartingBanner label={bannerLabel} />}
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
  pressed: { opacity: 0.6 },
});
