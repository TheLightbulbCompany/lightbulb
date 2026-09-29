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
  useColorScheme,
  View,
} from "react-native";

import { usePulse } from "../../../../components/chat/usePulse";
import { chatPalette, fonts, radii, spacing, type ChatPalette } from "../../../../config/theme";
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
  const styles = SHEETS[useColorScheme() === "dark" ? "dark" : "light"];
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
  const styles = SHEETS[useColorScheme() === "dark" ? "dark" : "light"];
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
  const scheme = useColorScheme() === "dark" ? "dark" : "light";
  const styles = SHEETS[scheme];
  const palette = chatPalette[scheme];
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
        { text: "Open Chats", onPress: () => router.navigate("/(app)/(tabs)/chats") },
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
          {free && (
            <View style={styles.priceBadge}>
              <Text style={styles.priceText}>{formatPrice(listing.price_cents)}</Text>
            </View>
          )}
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
              <ActivityIndicator size="small" color={palette.onFg} />
            ) : (
              <Text style={styles.primaryButtonText}>
                Deploy to my workspace
              </Text>
            )}
          </Pressable>
        ) : (
          // Plain text on purpose — no button, no link, no price, and no
          // mention of the web app: nothing that starts or points at a
          // purchase from inside the app (App Store 3.1.1).
          <Text style={styles.webOnlyNote}>Not available in the app.</Text>
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

function makeStyles(p: ChatPalette) {
  return StyleSheet.create({
    page: { flex: 1, backgroundColor: p.ground },
    scroll: { flex: 1 },
    scrollContent: {
      padding: spacing.lg,
      gap: spacing.lg,
      paddingBottom: spacing.xl,
    },

    headerBlock: { alignItems: "center", gap: spacing.sm },
    emoji: { fontSize: 44, lineHeight: 52 },
    title: {
      fontFamily: fonts.sansSemiBold,
      fontSize: 20,
      lineHeight: 26,
      color: p.fg,
      textAlign: "center",
    },
    byline: {
      fontFamily: fonts.mono,
      fontSize: 11,
      lineHeight: 15,
      letterSpacing: 0.4,
      color: p.fgMute,
    },
    vibe: {
      fontFamily: fonts.loraItalic,
      fontSize: 16,
      lineHeight: 24,
      color: p.fgSoft,
      textAlign: "center",
    },

    priceBadge: {
      paddingHorizontal: spacing.md,
      paddingVertical: spacing.xs,
      borderRadius: radii.sm,
      backgroundColor: p.raised,
    },
    priceText: {
      fontFamily: fonts.mono,
      fontSize: 11,
      lineHeight: 15,
      letterSpacing: 0.8,
      color: p.fg,
    },

    section: { gap: spacing.sm },
    sectionLabel: {
      fontFamily: fonts.sansMedium,
      fontSize: 10,
      lineHeight: 14,
      letterSpacing: 1.9,
      color: p.fgMute,
    },
    sectionCount: { color: p.fgMute },
    description: {
      fontFamily: fonts.sans,
      fontSize: 14,
      lineHeight: 21,
      color: p.fg,
    },
    selfContained: {
      fontFamily: fonts.sans,
      fontSize: 13,
      lineHeight: 18,
      color: p.fgMute,
    },

    // Requirements card.
    reqCard: {
      backgroundColor: p.surface,
      borderRadius: radii.md,
      borderWidth: 1,
      borderColor: p.edge,
      overflow: "hidden",
    },
    divider: {
      height: StyleSheet.hairlineWidth,
      backgroundColor: p.edge,
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
      color: p.fg,
    },
    reqNote: {
      fontFamily: fonts.sans,
      fontSize: 11,
      lineHeight: 15,
      color: p.fgMute,
    },
    reqEnv: {
      fontFamily: fonts.mono,
      fontSize: 10,
      lineHeight: 14,
      color: p.fgMute,
    },
    reqDisposition: {
      fontFamily: fonts.mono,
      fontSize: 9,
      lineHeight: 12,
      letterSpacing: 0.8,
      color: p.fg,
    },

    // Primary action.
    primaryButton: {
      alignItems: "center",
      justifyContent: "center",
      paddingVertical: spacing.md,
      borderRadius: radii.md,
      backgroundColor: p.fg,
    },
    primaryButtonDisabled: { opacity: 0.5 },
    primaryButtonText: {
      fontFamily: fonts.sansMedium,
      fontSize: 15,
      lineHeight: 20,
      color: p.onFg,
    },
    webOnlyNote: {
      fontFamily: fonts.sans,
      fontSize: 12,
      lineHeight: 16,
      color: p.fgMute,
      textAlign: "center",
    },

    // Skeleton (spec §7).
    skeletonGroup: { padding: spacing.lg, gap: spacing.md },
    skelTitle: {
      height: 26,
      width: "60%",
      borderRadius: radii.xs,
      backgroundColor: p.raised,
    },
    skelLine: {
      height: 14,
      borderRadius: radii.xs,
      backgroundColor: p.raised,
    },
    skelBlock: {
      height: 120,
      borderRadius: radii.md,
      backgroundColor: p.raised,
      marginTop: spacing.sm,
    },

    // Error card (spec §4).
    errorCard: {
      margin: spacing.md,
      padding: spacing.lg,
      backgroundColor: p.raised,
      borderWidth: 1,
      borderColor: p.alert,
      borderRadius: radii.md,
      gap: spacing.md,
    },
    errorText: {
      fontFamily: fonts.sans,
      fontSize: 14,
      lineHeight: 20,
      color: p.alert,
    },
    retryButton: {
      alignSelf: "flex-start",
      borderWidth: 1,
      borderColor: p.alert,
      borderRadius: radii.sm,
      backgroundColor: p.surface,
      paddingHorizontal: spacing.md,
      paddingVertical: spacing.xs,
    },
    retryText: {
      fontFamily: fonts.sansMedium,
      fontSize: 13,
      lineHeight: 18,
      color: p.alert,
    },
    pressed: { opacity: 0.6 },
  });
}

const SHEETS = { light: makeStyles(chatPalette.light), dark: makeStyles(chatPalette.dark) };
