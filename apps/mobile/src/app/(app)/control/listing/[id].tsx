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
  Linking,
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
  listingByline,
  MissingCredentialsError,
  PurchaseRequiredError,
  useCheckoutListing,
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
  const checkout = useCheckoutListing();

  useEffect(() => {
    void load();
  }, [load]);

  // Ref-latch against double-tap (deploy/checkout are non-idempotent — a
  // re-tap mints a second agent / a second Stripe session). Mirrors the
  // agents.create latch: checked + set synchronously before any await.
  const busyRef = useRef(false);
  const [busy, setBusy] = useState(false);

  const notifyDeployed = useCallback((detail: ListingDetail) => {
    Alert.alert(
      "Deployed",
      `${detail.listing.name} was added to your workspace.`,
      [
        { text: "Open chat", onPress: () => router.navigate("/(app)/chat") },
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

  // Primary action. Branches on ENTITLEMENT, not just price, so a buyer who
  // already owns a paid listing (or the seller of their own paid listing) is
  // NOT routed back through Stripe checkout and charged a second time:
  //
  //   • free OR seller-self → deploy directly, in-app, never any checkout.
  //   • paid + not-seller → attempt the in-app deploy FIRST. The backend runs
  //     its own entitlement check, so an owner (holding a purchase row)
  //     deploys for free. A deploy NEVER charges. Only a genuine
  //     PurchaseRequiredError (403 "purchase required") falls through to
  //     Stripe checkout in the system browser. Any other error (incl. the
  //     org-admin 403, which checkout would also reject) surfaces as an error.
  const onPrimary = useCallback(
    async (detail: ListingDetail) => {
      if (busyRef.current) return;
      busyRef.current = true;
      setBusy(true);
      try {
        const { listing } = detail;
        const free = !listing.price_cents || listing.price_cents <= 0;
        const isSeller = userId != null && listing.seller_id === userId;

        if (!free && !isSeller) {
          // Deploy-first: entitled owners deploy without a charge; only the
          // not-entitled path hands off to checkout.
          try {
            await deploy(listing.listing_id);
            notifyDeployed(detail);
            return;
          } catch (err) {
            if (!(err instanceof PurchaseRequiredError)) {
              notifyDeployError(err);
              return;
            }
            // Not entitled → fall through to Stripe checkout below.
          }
          try {
            const url = await checkout(listing.listing_id);
            // D1: NO in-app Stripe. Hand the Stripe-hosted URL to the system
            // browser; the web /deploy-success page completes the purchase.
            await Linking.openURL(url);
          } catch (err) {
            Alert.alert(
              "Couldn’t start checkout",
              err instanceof Error ? err.message : String(err),
            );
          }
          return;
        }

        // Free listing or seller-self deploy: fully in-app, never any charge.
        try {
          await deploy(listing.listing_id);
          notifyDeployed(detail);
        } catch (err) {
          notifyDeployError(err);
        }
      } finally {
        busyRef.current = false;
        setBusy(false);
      }
    },
    [deploy, checkout, userId, notifyDeployed, notifyDeployError],
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
    const isSeller = userId != null && listing.seller_id === userId;
    // In-app deploy with no possible charge: free listings, or the seller
    // deploying their own listing (backend seller-self bypass). Paid listings
    // for non-sellers show "Get" — deploy-first, checkout only if unentitled.
    const deployOnly = free || isSeller;
    const requirements = manifest?.requirements ?? [];
    const description =
      manifest?.description?.trim() || listing.description_md.trim();
    const emoji = manifest?.emoji?.trim();

    body = (
      <ScrollView
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

        <Pressable
          style={({ pressed }) => [
            styles.primaryButton,
            busy && styles.primaryButtonDisabled,
            pressed && !busy && styles.pressed,
          ]}
          onPress={() => void onPrimary(state.detail)}
          disabled={busy}
          accessibilityRole="button"
          accessibilityLabel={
            deployOnly ? "Deploy to my workspace" : "Get this agent"
          }
        >
          {busy ? (
            <ActivityIndicator size="small" color={colors.white} />
          ) : (
            <Text style={styles.primaryButtonText}>
              {deployOnly ? "Deploy to my workspace" : "Get"}
            </Text>
          )}
        </Pressable>

        {!deployOnly && (
          <Text style={styles.checkoutNote}>
            Free if you already own this. Otherwise checkout opens securely in
            your browser.
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
    fontFamily: fonts.dmSans,
    fontSize: 14,
    lineHeight: 21,
    color: colors.assistantInk,
  },
  selfContained: {
    fontFamily: fonts.dmSans,
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
    fontFamily: fonts.dmSansMedium,
    fontSize: 13,
    lineHeight: 18,
    color: colors.ink,
  },
  reqNote: {
    fontFamily: fonts.dmSans,
    fontSize: 11,
    lineHeight: 15,
    color: colors.mute,
  },
  reqEnv: {
    fontFamily: fonts.mono,
    fontSize: 10,
    lineHeight: 14,
    color: colors.muteSoft,
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
  primaryButtonDisabled: { backgroundColor: colors.creamPressed },
  primaryButtonText: {
    fontFamily: fonts.dmSansMedium,
    fontSize: 15,
    lineHeight: 20,
    color: colors.white,
  },
  checkoutNote: {
    fontFamily: fonts.dmSans,
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
  retryText: {
    fontFamily: fonts.dmSansMedium,
    fontSize: 13,
    lineHeight: 18,
    color: colors.errorText,
  },
  pressed: { opacity: 0.6 },
});
