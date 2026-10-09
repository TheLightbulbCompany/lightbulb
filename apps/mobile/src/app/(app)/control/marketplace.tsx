import { useAuth } from "@clerk/expo";
import { SymbolView } from "expo-symbols";
import { router } from "expo-router";
import { useEffect, useRef, useState, type ReactElement } from "react";
import {
  Animated,
  FlatList,
  Pressable,
  StyleSheet,
  Text,
  TextInput,
  useColorScheme,
  View,
} from "react-native";

import { usePulse } from "../../../components/chat/usePulse";
import { chatPalette, fonts, radii, spacing, type ChatPalette } from "../../../config/theme";
import {
  formatPrice,
  isFreeToHire,
  listingByline,
  listingSummary,
  useMarketplaceSearch,
  type Listing,
} from "../../../lib/marketplace";
import { LogoMark } from "../../../components/LogoMark";

// ---------------------------------------------------------------------------
// Skeleton (spec §7) — pulsing rows while the search is in flight.
// ---------------------------------------------------------------------------

function SkeletonRows() {
  const styles = SHEETS[useColorScheme() === "dark" ? "dark" : "light"];
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
  const scheme = useColorScheme() === "dark" ? "dark" : "light";
  const styles = SHEETS[scheme];
  const palette = chatPalette[scheme];
  const summary = listingSummary(item.description_md);
  const byline = listingByline(item);
  const free = !item.price_cents || item.price_cents <= 0;

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
          {free && (
            <View style={styles.priceBadge}>
              <Text style={styles.priceText}>{formatPrice(item.price_cents)}</Text>
            </View>
          )}
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
      <SymbolView accessibilityElementsHidden name="chevron.right" size={18} tintColor={palette.fgMute} />
    </Pressable>
  );
}

// ---------------------------------------------------------------------------
// Screen
// ---------------------------------------------------------------------------

export default function MarketplacePanel() {
  const scheme = useColorScheme() === "dark" ? "dark" : "light";
  const styles = SHEETS[scheme];
  const palette = chatPalette[scheme];
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
    // Same rule the server applied via free_only (there it runs before the
    // ranked limit, which is what keeps free listings from being truncated
    // away). This copy only bites if the app outruns a backend that ignores
    // the param — a paid row must never reach a shelf with no way to buy.
    const items = state.items.filter((l) => isFreeToHire(l, userId));
    body = (
      <FlatList
        contentInsetAdjustmentBehavior="automatic"
        automaticallyAdjustKeyboardInsets
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
            <LogoMark size={48} />
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
        <SymbolView accessibilityElementsHidden name="magnifyingglass" size={16} tintColor={palette.fgMute} />
        <TextInput
          style={styles.searchInput}
          value={query}
          onChangeText={setQuery}
          placeholder="Search agents"
          placeholderTextColor={palette.fgMute}
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
            <SymbolView accessibilityElementsHidden name="xmark" size={16} tintColor={palette.fgMute} />
          </Pressable>
        )}
      </View>
      {body}
    </View>
  );
}

function makeStyles(p: ChatPalette) {
  return StyleSheet.create({
    page: { flex: 1, backgroundColor: p.ground },

    // Search field.
    searchWrap: {
      flexDirection: "row",
      alignItems: "center",
      gap: spacing.sm,
      marginHorizontal: spacing.md,
      marginTop: spacing.md,
      paddingHorizontal: spacing.md,
      paddingVertical: 10,
      backgroundColor: p.raised,
      borderWidth: 1,
      borderColor: p.edge,
      borderRadius: radii.md,
    },
    searchInput: {
      flex: 1,
      fontFamily: fonts.sans,
      fontSize: 15,
      lineHeight: 20,
      color: p.fg,
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
    dim: {
      fontFamily: fonts.sans,
      fontSize: 15,
      lineHeight: 22,
      color: p.fgMute,
      textAlign: "center",
    },

    sectionLabel: {
      fontFamily: fonts.sansMedium,
      fontSize: 10,
      lineHeight: 14,
      letterSpacing: 1.9,
      color: p.fgMute,
      paddingHorizontal: spacing.sm,
      marginBottom: spacing.sm,
    },
    sectionCount: { color: p.fgMute },

    // Listing row.
    row: {
      flexDirection: "row",
      alignItems: "center",
      gap: spacing.sm,
      padding: spacing.md,
      borderRadius: radii.md,
    },
    rowPressed: { backgroundColor: p.raised },
    rowText: { flex: 1, gap: 2 },
    nameRow: { flexDirection: "row", alignItems: "center", gap: spacing.sm },
    name: {
      fontFamily: fonts.sansMedium,
      fontSize: 13,
      lineHeight: 18,
      color: p.fg,
      flexShrink: 1,
    },
    summary: {
      fontFamily: fonts.sans,
      fontSize: 11,
      lineHeight: 15,
      color: p.fgMute,
    },
    author: {
      fontFamily: fonts.mono,
      fontSize: 10,
      lineHeight: 14,
      letterSpacing: 0.4,
      color: p.fgMute,
    },

    // Price badge — FREE only (App Store 3.1.1: no price shown for paid rows).
    priceBadge: {
      paddingHorizontal: spacing.sm,
      paddingVertical: 2,
      borderRadius: radii.xs,
      backgroundColor: p.raised,
      borderWidth: 1,
      borderColor: p.edge,
    },
    priceText: {
      fontFamily: fonts.mono,
      fontSize: 9,
      lineHeight: 12,
      letterSpacing: 0.8,
      color: p.fg,
    },

    // Empty state (spec §7).
    emptyWrap: { alignItems: "center", gap: spacing.sm },

    // Skeletons (spec §7).
    skeletonGroup: { padding: spacing.md, gap: spacing.sm },
    skeletonRow: {
      height: 60,
      borderRadius: radii.md,
      backgroundColor: p.raised,
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
