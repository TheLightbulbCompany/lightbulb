import { SymbolView } from "../ui/SymbolView";
import { ALL_EMOJI, EMOJI_CATEGORIES, searchEmoji, type EmojiEntry } from "@lightbulb/shared";
import { useCallback, useEffect, useMemo, useState } from "react";
import { FlatList, Pressable, StyleSheet, Text, TextInput, useWindowDimensions, View } from "react-native";

import { fonts, radii, spacing, type, useChatPalette } from "../../config/theme";
import { hapticSelection } from "../../lib/haptics";
import { LocalStore } from "../../lib/localStore";
import { BottomSheet } from "../ui/BottomSheet";

/** Mirrors the web `EmojiPicker`'s own `RECENT_MAX` (`RECENT_EMOJI_KEY`, 8). */
const RECENT_MAX = 8;

/**
 * ~44pt cells (Apple's minimum tap target). Column count is derived from
 * screen width so the grid fills it edge to edge on every phone size.
 */
const CELL_SIZE = 44;

const recentsStore = new LocalStore<{ recents: string[] }>("emoji-recents");

/** One row of the FlatList: either a full-width category label or a chunk of
 *  `columns` emoji. Pre-chunking rows (rather than handing the shared, flat
 *  entry list to `FlatList`'s own `numColumns`) is what lets a category
 *  header sit above its own grid — `numColumns` places every item in an
 *  N-per-row grid uniformly and has no way to let one item span the row. */
type Row =
  | { type: "header"; key: string; title: string }
  | { type: "cells"; key: string; items: readonly EmojiEntry[] };

function chunk<T>(items: readonly T[], size: number): T[][] {
  const rows: T[][] = [];
  for (let i = 0; i < items.length; i += size) rows.push(items.slice(i, i + size) as T[]);
  return rows;
}

/**
 * The composer's emoji picker: a full-height native sheet with search-filtered
 * or categorized browsing, a recents row, and a virtualized grid (this is
 * `MessageActionsSheet`'s promised "Phase B's emoji sheet").
 *
 * `large` because it carries a text field above a list — the sheet must fill
 * the screen so the keyboard shrinks the grid instead of pushing the whole
 * card, and the same reason `NewMessageSheet` picked it.
 *
 * Recents persist across app launches via `LocalStore` (per-device, not
 * per-viewer synced) — the mobile analogue of the web picker's
 * `localStorage`-backed `recordRecentEmoji`.
 */
export function EmojiPickerSheet({
  visible,
  onClose,
  onPick,
}: {
  visible: boolean;
  onClose: () => void;
  onPick: (emoji: string) => void;
}) {
  const palette = useChatPalette();
  const { width } = useWindowDimensions();
  // Not derived from `fontScale`: the glyphs below are capped at
  // `maxFontSizeMultiplier={1}` (chrome in a fixed cell, not content — the
  // same call `Avatar.tsx` makes for its own emoji), so a larger accessibility
  // text size doesn't need a coarser grid to keep them legible or tappable —
  // the cell stays a fixed, already-≥44pt target either way.
  const columns = Math.max(6, Math.floor((width - spacing.lg * 2) / CELL_SIZE));
  // The columns share the row's width, so the grid ends where it began: at a
  // fixed 44 it was set against the left with what was over on the right.
  const cellWidth = (width - spacing.sm * 2) / columns;
  const [query, setQuery] = useState("");
  const [recents, setRecents] = useState<string[]>([]);

  // Opening is what (re)loads recents and clears the last visit's search —
  // matches `NewMessageSheet`'s own reset-on-open contract.
  useEffect(() => {
    if (!visible) return;
    setQuery("");
    setRecents(recentsStore.read().recents ?? []);
  }, [visible]);

  const pick = useCallback(
    (emoji: string) => {
      hapticSelection();
      const next = [
        emoji,
        ...(recentsStore.read().recents ?? []).filter((entry) => entry !== emoji),
      ].slice(0, RECENT_MAX);
      recentsStore.write({ recents: next });
      setRecents(next);
      onPick(emoji);
    },
    [onPick],
  );

  const rows = useMemo<Row[]>(() => {
    const needle = query.trim();
    if (needle) {
      return chunk(searchEmoji(needle), columns).map((items, index) => ({
        type: "cells" as const,
        key: `result-${index}`,
        items,
      }));
    }
    const recentEntries = recents
      .map((emoji) => ALL_EMOJI.find((entry) => entry.emoji === emoji))
      .filter((entry): entry is EmojiEntry => entry !== undefined);
    const sections = [
      ...(recentEntries.length ? [{ name: "Recent", entries: recentEntries }] : []),
      ...EMOJI_CATEGORIES,
    ];
    return sections.flatMap((section) => [
      { type: "header" as const, key: `header-${section.name}`, title: section.name },
      ...chunk(section.entries, columns).map((items, index) => ({
        type: "cells" as const,
        key: `${section.name}-${index}`,
        items,
      })),
    ]);
  }, [query, recents, columns]);

  return (
    <BottomSheet visible={visible} onClose={onClose} large>
      <View style={[styles.card, { backgroundColor: palette.surface }]}>
        <View style={[styles.searchPill, { backgroundColor: palette.raised }]}>
          <SymbolView
            accessibilityElementsHidden
            name="magnifyingglass"
            size={14}
            tintColor={palette.fgMute}
          />
          <TextInput
            style={[styles.searchInput, { color: palette.fg }]}
            placeholder="Search emoji"
            placeholderTextColor={palette.fgMute}
            value={query}
            onChangeText={setQuery}
            autoCorrect={false}
            autoCapitalize="none"
            clearButtonMode="while-editing"
            accessibilityLabel="Search emoji"
          />
        </View>
        <FlatList
          data={rows}
          keyExtractor={(row) => row.key}
          keyboardShouldPersistTaps="handled"
          contentContainerStyle={styles.list}
          ListEmptyComponent={<Text style={[styles.dim, { color: palette.fgMute }]}>No matching emoji.</Text>}
          renderItem={({ item }) =>
            item.type === "header" ? (
              <Text style={[styles.sectionHeader, { color: palette.fgMute }]}>{item.title}</Text>
            ) : (
              <View style={styles.row}>
                {item.items.map((entry) => (
                  <Pressable
                    key={entry.emoji}
                    onPress={() => pick(entry.emoji)}
                    accessibilityRole="button"
                    accessibilityLabel={entry.name}
                    style={({ pressed }) => [styles.cell, { width: cellWidth }, pressed && { backgroundColor: palette.hover }]}
                  >
                    <Text style={styles.glyph} maxFontSizeMultiplier={1}>
                      {entry.emoji}
                    </Text>
                  </Pressable>
                ))}
              </View>
            )
          }
        />
      </View>
    </BottomSheet>
  );
}

const styles = StyleSheet.create({
  card: {
    flex: 1,
    borderTopLeftRadius: radii.xl,
    borderTopRightRadius: radii.xl,
  },
  searchPill: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.sm,
    marginHorizontal: spacing.lg,
    marginBottom: spacing.sm,
    paddingHorizontal: spacing.md,
    height: 36,
    borderRadius: radii.md,
  },
  searchInput: {
    flex: 1,
    fontFamily: fonts.sans,
    fontSize: 15,
    padding: 0,
  },
  list: { paddingHorizontal: spacing.sm, paddingBottom: spacing.lg },
  sectionHeader: {
    ...type.caption,
    fontFamily: fonts.sansMedium,
    paddingHorizontal: spacing.sm,
    paddingTop: spacing.md,
    paddingBottom: spacing.xs,
  },
  row: { flexDirection: "row", flexWrap: "wrap" },
  cell: {
    width: CELL_SIZE,
    height: CELL_SIZE,
    alignItems: "center",
    justifyContent: "center",
    borderRadius: radii.md,
  },
  glyph: { fontSize: 26 },
  dim: { ...type.body, padding: spacing.lg, textAlign: "center" },
});
