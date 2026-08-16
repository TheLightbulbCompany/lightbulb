import { useAuth } from "@clerk/clerk-expo";
import Feather from "@expo/vector-icons/Feather";
import { router } from "expo-router";
import { useEffect, useRef, useState, type ReactElement } from "react";
import {
  Animated,
  FlatList,
  Pressable,
  StyleSheet,
  Text,
  TextInput,
  View,
} from "react-native";

import { usePulse } from "../../../components/chat/usePulse";
import { colors, fonts, radii, spacing, type } from "../../../config/theme";
import {
  formatPrice,
  isFreeToHire,
  listingByline,
  listingSummary,
  useMarketplaceSearch,
  type Listing,
} from "../../../lib/marketplace";

// ---------------------------------------------------------------------------
// Skeleton (spec §7) — pulsing rows while the search is in flight.
// ---------------------------------------------------------------------------

function SkeletonRows() {
  const pulse = usePulse(true, 0.55);
  return (
    <View style={styles.skeletonGroup}>
      {[0, 1, 2, 3].map((i) => (
        <Animated.View key={i} style={[styles.skeletonRow, { opacity: pulse }]} />
      ))}
    </View>
  );
}

// ---------------------------------------------------------------------------
// Listing row (spec §5) — name / summary / author + price badge.
// ---------------------------------------------------------------------------

function ListingRow({ item }: { item: Listing }) {
  const summary = listingSummary(item.description_md);
  const byline = listingByline(item);
  const free = !item.price_cents || item.price_cents <= 0;
  const price = formatPrice(item.price_cents);

  return (
    <Pressable
      style={({ pressed }) => [styles.row, pressed && styles.rowPressed]}
      onPress={() =>
        router.push({
          pathname: "/(app)/control/listing/[id]",
          params: { id: item.listing_id },
        })
      }
      accessibilityRole="button"
      accessibilityLabel={item.name}
    >
      <View style={styles.rowText}>
        <View style={styles.nameRow}>
          <Text style={styles.name} numberOfLines={1}>
            {item.name}
          </Text>
          <View style={[styles.priceBadge, !free && styles.priceBadgePaid]}>
            <Text style={[styles.priceText, !free && styles.priceTextPaid]}>
              {price}
            </Text>
          </View>
        </View>
        {summary !== "" && (
          <Text style={styles.summary} numberOfLines={1}>
            {summary}
          </Text>
        )}
        <Text style={styles.author} numberOfLines={1}>
          {byline}
        </Text>
      </View>
      <Feather name="chevron-right" size={18} color={colors.muteSoft} />
    </Pressable>
  );
}

// ---------------------------------------------------------------------------
// Screen
// ---------------------------------------------------------------------------

export default function MarketplacePanel() {
  const { state, search } = useMarketplaceSearch();
  const { userId } = useAuth();
  const [query, setQuery] = useState("");

  // Debounce keystrokes → one search per 300ms of quiet. The ref keeps the
  // latest `search` callback stable across renders without re-arming the timer.
  const searchRef = useRef(search);
  searchRef.current = search;
  useEffect(() => {
    const id = setTimeout(() => {
      void searchRef.current(query);
    }, 300);
    return () => clearTimeout(id);
  }, [query]);

  let body: ReactElement;
  if (state.kind === "error") {
    body = (
      <View style={styles.errorCard}>
        <Text style={styles.errorText}>
          Couldn’t load the marketplace: {state.message}
        </Text>
        <Pressable
          style={({ pressed }) => [styles.retryButton, pressed && styles.pressed]}
          onPress={() => void search(query)}
          accessibilityRole="button"
          accessibilityLabel="Retry"
        >
          <Text style={styles.retryText}>Retry</Text>
        </Pressable>
      </View>
    );
  } else if (state.kind === "loading") {
    body = <SkeletonRows />;
  } else {
    // Belt on the server's free_only filter (which is what actually keeps
    // free listings from being truncated away by the ranked limit). This
    // only bites if the app outruns a backend that ignores the param — a
    // paid row must never reach a shelf with no in-app way to buy.
    const items = state.items.filter((l) => isFreeToHire(l, userId));
    body = (
      <FlatList
        data={items}
        keyExtractor={(l) => l.listing_id}
        style={styles.list}
        contentContainerStyle={[
          styles.listContent,
          items.length === 0 && styles.centerGrow,
        ]}
        ListHeaderComponent={
          items.length > 0 ? (
            <Text style={styles.sectionLabel}>
              LISTINGS
              <Text style={styles.sectionCount}> · {items.length}</Text>
            </Text>
          ) : null
        }
        ListEmptyComponent={
          <View style={styles.emptyWrap}>
            <Text style={styles.emptyGlyph}>8</Text>
            <Text style={styles.dim}>No listings</Text>
          </View>
        }
        renderItem={({ item }) => <ListingRow item={item} />}
        keyboardShouldPersistTaps="handled"
      />
    );
  }

  return (
    <View style={styles.page}>
      <View style={styles.searchWrap}>
        <Feather name="search" size={16} color={colors.mute} />
        <TextInput
          style={styles.searchInput}
          value={query}
          onChangeText={setQuery}
          placeholder="Search agents"
          placeholderTextColor={colors.placeholder}
          autoCapitalize="none"
          autoCorrect={false}
          returnKeyType="search"
          onSubmitEditing={() => void search(query)}
        />
        {query !== "" && (
          <Pressable
            onPress={() => setQuery("")}
            hitSlop={8}
            accessibilityRole="button"
            accessibilityLabel="Clear search"
          >
            <Feather name="x" size={16} color={colors.mute} />
          </Pressable>
        )}
      </View>
      {body}
    </View>
  );
}

const styles = StyleSheet.create({
  page: { flex: 1, backgroundColor: colors.cream },

  // Search field.
  searchWrap: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.sm,
    marginHorizontal: spacing.md,
    marginTop: spacing.md,
    paddingHorizontal: spacing.md,
    paddingVertical: 10,
    backgroundColor: colors.white,
    borderWidth: 1,
    borderColor: colors.edge,
    borderRadius: radii.md,
  },
  searchInput: {
    flex: 1,
    fontFamily: fonts.dmSans,
    fontSize: 15,
    lineHeight: 20,
    color: colors.ink,
    padding: 0,
  },

  list: { flex: 1 },
  listContent: {
    paddingHorizontal: spacing.md,
    paddingTop: spacing.md,
    paddingBottom: spacing.xl,
  },
  centerGrow: {
    flexGrow: 1,
    alignItems: "center",
    justifyContent: "center",
    padding: spacing.xl,
  },
  dim: { ...type.body, color: colors.mute, textAlign: "center" },

  sectionLabel: {
    ...type.overline,
    paddingHorizontal: spacing.sm,
    marginBottom: spacing.sm,
  },
  sectionCount: { color: colors.sectionCount },

  // Listing row.
  row: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.sm,
    padding: spacing.md,
    borderRadius: radii.md,
  },
  rowPressed: { backgroundColor: colors.rowPressed },
  rowText: { flex: 1, gap: 2 },
  nameRow: { flexDirection: "row", alignItems: "center", gap: spacing.sm },
  name: {
    fontFamily: fonts.dmSansMedium,
    fontSize: 13,
    lineHeight: 18,
    color: colors.ink,
    flexShrink: 1,
  },
  summary: {
    fontFamily: fonts.dmSans,
    fontSize: 11,
    lineHeight: 15,
    color: colors.mute,
  },
  author: {
    fontFamily: fonts.mono,
    fontSize: 10,
    lineHeight: 14,
    letterSpacing: 0.4,
    color: colors.muteSoft,
  },

  // Price badge — FREE (forest tint) vs paid ($ amount, gold).
  priceBadge: {
    paddingHorizontal: spacing.sm,
    paddingVertical: 2,
    borderRadius: radii.xs,
    backgroundColor: colors.forestTint,
  },
  priceBadgePaid: { backgroundColor: colors.chip },
  priceText: {
    fontFamily: fonts.mono,
    fontSize: 9,
    lineHeight: 12,
    letterSpacing: 0.8,
    color: colors.forest,
  },
  priceTextPaid: { color: colors.inkSoft },

  // Empty state (spec §7).
  emptyWrap: { alignItems: "center", gap: spacing.sm },
  emptyGlyph: {
    fontFamily: fonts.loraItalic,
    fontSize: 48,
    lineHeight: 56,
    color: colors.ink,
  },

  // Skeletons (spec §7).
  skeletonGroup: { padding: spacing.md, gap: spacing.sm },
  skeletonRow: {
    height: 60,
    borderRadius: radii.md,
    backgroundColor: colors.skeleton,
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
