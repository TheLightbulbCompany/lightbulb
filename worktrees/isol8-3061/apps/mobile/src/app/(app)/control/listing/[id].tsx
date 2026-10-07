import { useAuth } from "@clerk/clerk-expo";
import { router, Stack, useLocalSearchParams } from "expo-router";
import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type ReactElement,
} from "react";
import {
  ActivityIndicator,
  Alert,
  Animated,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  View,
} from "react-native";

import { usePulse } from "../../../../components/chat/usePulse";
import { colors, fonts, radii, spacing, type } from "../../../../config/theme";
import {
  formatPrice,
  isFreeToHire,
  listingByline,
  MissingCredentialsError,
  useDeployListing,
  useListingDetail,
  type ListingDetail,
  type RequirementRow,
} from "../../../../lib/marketplace";

// ---------------------------------------------------------------------------
// Skeleton (spec §7)
// ---------------------------------------------------------------------------

function DetailSkeleton() {
  const pulse = usePulse(true, 0.55);
  return (
    <View style={styles.skeletonGroup}>
      <Animated.View style={[styles.skelTitle, { opacity: pulse }]} />
      <Animated.View style={[styles.skelLine, { opacity: pulse, width: "90%" }]} />
      <Animated.View style={[styles.skelLine, { opacity: pulse, width: "70%" }]} />
      <Animated.View style={[styles.skelBlock, { opacity: pulse }]} />
    </View>
  );
}

// ---------------------------------------------------------------------------
// Requirement row (manifest.requirements[])
// ---------------------------------------------------------------------------

const DISPOSITION_LABEL: Record<RequirementRow["disposition"], string> = {
  included: "Included",
  connect: "Connect",
  byo: "Bring your own",
  unavailable: "Unavailable",
};

function RequirementItem({ req }: { req: RequirementRow }) {
  return (
    <View style={styles.reqRow}>
      <View style={styles.reqText}>
        <Text style={styles.reqName} numberOfLines={1}>
          {req.display_name}
        </Text>
        {req.note != null && req.note !== "" && (
          <Text style={styles.reqNote} numberOfLines={2}>
            {req.note}
          </Text>
        )}
        {req.env.length > 0 && (
          <Text style={styles.reqEnv} numberOfLines={1}>
            {req.env.join(", ")}
          </Text>
        )}
      </View>
      <Text style={styles.reqDisposition}>
        {DISPOSITION_LABEL[req.disposition] ?? req.disposition}
      </Text>
    </View>
  );
}

// ---------------------------------------------------------------------------
// Screen
// ---------------------------------------------------------------------------

export default function ListingDetailScreen() {
  const { id } = useLocalSearchParams<{ id: string }>();
  const { userId } = useAuth();
  const { state, load } = useListingDetail(id);
  const deploy = useDeployListing();

  useEffect(() => {
    void load();
  }, [load]);

  // Ref-latch against double-tap (deploy is non-idempotent — a re-tap mints a
  // second agent). Mirrors the agents.create latch: checked + set
  // synchronously before any await.
  const busyRef = useRef(false);
  const [busy, setBusy] = useState(false);

  const notifyDeployed = useCallback((detail: ListingDetail) => {
    Alert.alert(
      "Deployed",
      `${detail.listing.name} was added to your workspace.`,
      [
        { text: "Open Home", onPress: () => router.navigate("/(app)/(tabs)/home") },
        { text: "Done", style: "cancel" },
      ],
    );
  }, []);

  const notifyDeployError = useCallback((err: unknown) => {
    if (err instanceof MissingCredentialsError) {
      Alert.alert(
        "Connect a provider first",
        "This agent needs an AI provider before it can run. Open Model & Credits in Control to connect one, then try again.",
      );
    } else {
      Alert.alert(
        "Couldn’t deploy",
        err instanceof Error ? err.message : String(err),
      );
    }
  }, []);

  // The only action on this screen: an in-app deploy, which never charges.
  // It is rendered exclusively for listings isFreeToHire() accepts, so no tap
  // here can reach a purchase.
  const onPrimary = useCallback(
    async (detail: ListingDetail) => {
      if (busyRef.current) return;
      busyRef.current = true;
      setBusy(true);
      try {
        await deploy(detail.listing.listing_id);
        notifyDeployed(detail);
      } catch (err) {
        notifyDeployError(err);
      } finally {
        busyRef.current = false;
        setBusy(false);
      }
    },
    [deploy, notifyDeployed, notifyDeployError],
  );

  let body: ReactElement;
  let headerTitle = "Listing";

  if (state.kind === "loading") {
    body = <DetailSkeleton />;
  } else if (state.kind === "error") {
    body = (
      <View style={styles.errorCard}>
        <Text style={styles.errorText}>
          Couldn’t load this listing: {state.message}
        </Text>
        <Pressable
          style={({ pressed }) => [styles.retryButton, pressed && styles.pressed]}
          onPress={() => void load()}
          accessibilityRole="button"
          accessibilityLabel="Retry"
        >
          <Text style={styles.retryText}>Retry</Text>
        </Pressable>
      </View>
    );
  } else {
    const { listing, manifest } = state.detail;
    headerTitle = listing.name;
    const free = !listing.price_cents || listing.price_cents <= 0;
    // Hireable from the phone: free listings, or the seller deploying their
    // own (backend seller-self bypass) — both are in-app deploys with no
    // possible charge. Anything else is web-only; search filters those out,
    // but a deep link can still land here.
    const deployable = isFreeToHire(listing, userId);
    const requirements = manifest?.requirements ?? [];
    const description =
      manifest?.description?.trim() || listing.description_md.trim();
    const emoji = manifest?.emoji?.trim();

    body = (
      <ScrollView
        contentInsetAdjustmentBehavior="automatic"
        style={styles.scroll}
        contentContainerStyle={styles.scrollContent}
      >
        <View style={styles.headerBlock}>
          {emoji ? <Text style={styles.emoji}>{emoji}</Text> : null}
          <Text style={styles.title}>{listing.name}</Text>
          <Text style={styles.byline}>{listingByline(listing)}</Text>
          <View style={[styles.priceBadge, !free && styles.priceBadgePaid]}>
            <Text style={[styles.priceText, !free && styles.priceTextPaid]}>
              {formatPrice(listing.price_cents)}
            </Text>
          </View>
        </View>

        {manifest?.vibe != null && manifest.vibe.trim() !== "" && (
          <Text style={styles.vibe}>{manifest.vibe.trim()}</Text>
        )}

        {description !== "" && (
          <View style={styles.section}>
            <Text style={styles.sectionLabel}>ABOUT</Text>
            <Text style={styles.description}>{description}</Text>
          </View>
        )}

        <View style={styles.section}>
          <Text style={styles.sectionLabel}>
            REQUIREMENTS
            {requirements.length > 0 && (
              <Text style={styles.sectionCount}> · {requirements.length}</Text>
            )}
          </Text>
          {requirements.length === 0 ? (
            <Text style={styles.selfContained}>
              Self-contained — no external connections required.
            </Text>
          ) : (
            <View style={styles.reqCard}>
              {requirements.map((req, i) => (
                <View key={req.key}>
                  {i > 0 && <View style={styles.divider} />}
                  <RequirementItem req={req} />
                </View>
              ))}
            </View>
          )}
        </View>

        {deployable ? (
          <Pressable
            style={({ pressed }) => [
              styles.primaryButton,
              busy && styles.primaryButtonDisabled,
              pressed && !busy && styles.pressed,
            ]}
            onPress={() => void onPrimary(state.detail)}
            disabled={busy}
            accessibilityRole="button"
            accessibilityLabel="Deploy to my workspace"
          >
            {busy ? (
              <ActivityIndicator size="small" color={colors.white} />
            ) : (
              <Text style={styles.primaryButtonText}>
                Deploy to my workspace
              </Text>
            )}
          </Pressable>
        ) : (
          // Plain text on purpose — no button, no link, nothing that starts a
          // purchase from inside the app.
          <Text style={styles.webOnlyNote}>
            Paid agents are hired from the web app at flickmeon.ai.
          </Text>
        )}
      </ScrollView>
    );
  }

  return (
    <View style={styles.page}>
      <Stack.Screen options={{ title: headerTitle }} />
      {body}
    </View>
  );
}

const styles = StyleSheet.create({
  page: { flex: 1, backgroundColor: colors.cream },
  scroll: { flex: 1 },
  scrollContent: {
    padding: spacing.lg,
    gap: spacing.lg,
    paddingBottom: spacing.xl,
  },

  headerBlock: { alignItems: "center", gap: spacing.sm },
  emoji: { fontSize: 44, lineHeight: 52 },
  title: { ...type.title, textAlign: "center" },
  byline: {
    fontFamily: fonts.mono,
    fontSize: 11,
    lineHeight: 15,
    letterSpacing: 0.4,
    color: colors.mute,
  },
  vibe: {
    fontFamily: fonts.loraItalic,
    fontSize: 16,
    lineHeight: 24,
    color: colors.inkSoft,
    textAlign: "center",
  },

  priceBadge: {
    paddingHorizontal: spacing.md,
    paddingVertical: spacing.xs,
    borderRadius: radii.sm,
    backgroundColor: colors.forestTint,
  },
  priceBadgePaid: { backgroundColor: colors.chip },
  priceText: {
    fontFamily: fonts.mono,
    fontSize: 11,
    lineHeight: 15,
    letterSpacing: 0.8,
    color: colors.forest,
  },
  priceTextPaid: { color: colors.inkSoft },

  section: { gap: spacing.sm },
  sectionLabel: { ...type.overline },
  sectionCount: { color: colors.sectionCount },
  description: {
    fontFamily: fonts.sans,
    fontSize: 14,
    lineHeight: 21,
    color: colors.assistantInk,
  },
  selfContained: {
    fontFamily: fonts.sans,
    fontSize: 13,
    lineHeight: 18,
    color: colors.mute,
  },

  // Requirements card.
  reqCard: {
    backgroundColor: colors.white,
    borderRadius: radii.md,
    borderWidth: 1,
    borderColor: colors.edge,
    overflow: "hidden",
  },
  divider: {
    height: StyleSheet.hairlineWidth,
    backgroundColor: colors.edge,
    marginLeft: spacing.md,
  },
  reqRow: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.md,
    paddingHorizontal: spacing.md,
    paddingVertical: spacing.md,
  },
  reqText: { flex: 1, gap: 2 },
  reqName: {
    fontFamily: fonts.sansMedium,
    fontSize: 13,
    lineHeight: 18,
    color: colors.ink,
  },
  reqNote: {
    fontFamily: fonts.sans,
    fontSize: 11,
    lineHeight: 15,
    color: colors.mute,
  },
  reqEnv: {
    fontFamily: fonts.mono,
    fontSize: 10,
    lineHeight: 14,
    color: colors.mute,
  },
  reqDisposition: {
    fontFamily: fonts.mono,
    fontSize: 9,
    lineHeight: 12,
    letterSpacing: 0.8,
    color: colors.forest,
  },

  // Primary action.
  primaryButton: {
    alignItems: "center",
    justifyContent: "center",
    paddingVertical: spacing.md,
    borderRadius: radii.md,
    backgroundColor: colors.forest,
  },
  primaryButtonDisabled: { opacity: 0.5 },
  primaryButtonText: {
    fontFamily: fonts.sansMedium,
    fontSize: 15,
    lineHeight: 20,
    color: colors.white,
  },
  webOnlyNote: {
    fontFamily: fonts.sans,
    fontSize: 12,
    lineHeight: 16,
    color: colors.mute,
    textAlign: "center",
  },

  // Skeleton (spec §7).
  skeletonGroup: { padding: spacing.lg, gap: spacing.md },
  skelTitle: {
    height: 26,
    width: "60%",
    borderRadius: radii.xs,
    backgroundColor: colors.skeleton,
  },
  skelLine: {
    height: 14,
    borderRadius: radii.xs,
    backgroundColor: colors.skeleton,
  },
  skelBlock: {
    height: 120,
    borderRadius: radii.md,
    backgroundColor: colors.skeleton,
    marginTop: spacing.sm,
  },

  // Error card (spec §4).
  errorCard: {
    margin: spacing.md,
    padding: spacing.lg,
    backgroundColor: colors.errorBg,
    borderWidth: 1,
    borderColor: colors.errorBorder,
    borderRadius: radii.md,
    gap: spacing.md,
  },
  errorText: {
    fontFamily: fonts.sans,
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
  retryText: {
    fontFamily: fonts.sansMedium,
    fontSize: 13,
    lineHeight: 18,
    color: colors.errorText,
  },
  pressed: { opacity: 0.6 },
});
