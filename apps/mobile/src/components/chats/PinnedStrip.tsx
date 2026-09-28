import { sessionRingState, sessionToolName, type AvatarRing } from "@lightbulb/shared";
import { SymbolView } from "expo-symbols";
import {
  ActionSheetIOS,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  View,
} from "react-native";

import { fonts, spacing, type ChatPalette } from "../../config/theme";
import type { PinKey, PinnedItem } from "../../lib/chats";
import { Avatar } from "../room/Avatar";
import { ChannelTile } from "./ChatRow";

const TILE_SIZE = 56;
const RING_WORD: Record<AvatarRing, string> = {
  running: "working",
  attention: "needs attention",
  paused: "paused",
};

/** An item's ring, or null (rooms other than a solo agent DM never ring). */
function pinRing(item: PinnedItem): AvatarRing | null {
  if (item.kind === "agent") return item.ring;
  if (item.kind === "session") return sessionRingState(item.session);
  return null;
}

function PinnedTile({ item, ring, palette }: { item: PinnedItem; ring: AvatarRing | null; palette: ChatPalette }) {
  if (item.kind === "agent") {
    return <Avatar name={item.name} kind="agent" ring={ring} size={TILE_SIZE} palette={palette} />;
  }
  if (item.kind === "session") {
    return (
      <Avatar
        name={item.name}
        kind="agent"
        vendor={item.session.provider}
        ring={ring}
        size={TILE_SIZE}
        palette={palette}
      />
    );
  }
  if (item.room.kind === "channel") return <ChannelTile size={TILE_SIZE} palette={palette} />;
  const other = item.others[0];
  return <Avatar name={other?.displayName ?? item.name} kind={other?.kind ?? "human"} size={TILE_SIZE} palette={palette} />;
}

function PinnedColumn({
  item,
  palette,
  reordering,
  isFirst,
  isLast,
  onOpen,
  onUnpin,
  onMove,
  onReorder,
}: {
  item: PinnedItem;
  palette: ChatPalette;
  reordering: boolean;
  isFirst: boolean;
  isLast: boolean;
  onOpen: (item: PinnedItem) => void;
  onUnpin: (key: PinKey) => void;
  onMove: (key: PinKey, delta: -1 | 1) => void;
  onReorder: (on: boolean) => void;
}) {
  const ring = pinRing(item);
  // A session tile shows only its title; the tool is the logo, so say it aloud.
  const who = item.kind === "session" ? `${sessionToolName(item.session.provider)} · ${item.name}` : item.name;
  const label = `${who}${ring ? `, ${RING_WORD[ring]}` : ""}`;
  // Sessions can't be pinned/reordered — no menu, no accessibility actions.
  const manageable = item.kind !== "session";

  const openSheet = () => {
    ActionSheetIOS.showActionSheetWithOptions(
      { options: ["Unpin", "Reorder pins", "Cancel"], destructiveButtonIndex: 0, cancelButtonIndex: 2, title: item.name },
      (index) => {
        if (index === 0) onUnpin(item.key as PinKey);
        else if (index === 1) onReorder(true);
      },
    );
  };

  return (
    <View style={styles.column}>
      <Pressable
        onPress={() => onOpen(item)}
        onLongPress={manageable ? openSheet : undefined}
        disabled={reordering}
        accessibilityRole="button"
        accessibilityLabel={label}
        accessibilityActions={
          manageable ? [{ name: "unpin", label: "Unpin" }, { name: "reorder", label: "Reorder pins" }] : undefined
        }
        onAccessibilityAction={
          manageable
            ? (event) => {
                if (event.nativeEvent.actionName === "unpin") onUnpin(item.key as PinKey);
                if (event.nativeEvent.actionName === "reorder") onReorder(true);
              }
            : undefined
        }
      >
        <PinnedTile item={item} ring={ring} palette={palette} />
      </Pressable>
      {reordering ? (
        <View style={styles.moveRow}>
          <Pressable
            onPress={() => onMove(item.key as PinKey, -1)}
            disabled={isFirst}
            hitSlop={8}
            accessibilityRole="button"
            accessibilityLabel={`Move ${item.name} left`}
            accessibilityState={{ disabled: isFirst }}
          >
            <SymbolView name="chevron.left" size={28} tintColor={isFirst ? palette.fgMute : palette.fg} />
          </Pressable>
          <Pressable
            onPress={() => onMove(item.key as PinKey, 1)}
            disabled={isLast}
            hitSlop={8}
            accessibilityRole="button"
            accessibilityLabel={`Move ${item.name} right`}
            accessibilityState={{ disabled: isLast }}
          >
            <SymbolView name="chevron.right" size={28} tintColor={isLast ? palette.fgMute : palette.fg} />
          </Pressable>
        </View>
      ) : (
        <Text style={[styles.name, { color: palette.fgSoft }]} numberOfLines={1}>
          {item.name}
        </Text>
      )}
    </View>
  );
}

/**
 * The pinned strip atop Chats: agents, rooms and (when the switch is on) busy
 * sessions, as 72pt columns. Long-press an agent/room pin for Unpin or
 * Reorder pins; reorder mode swaps every pin's name for move-left/move-right
 * buttons (VoiceOver-friendly, no drag) and hides sessions, which can't be
 * pinned or reordered.
 */
export function PinnedStrip({
  items,
  palette,
  reordering,
  onOpen,
  onUnpin,
  onMove,
  onReorder,
}: {
  items: readonly PinnedItem[];
  palette: ChatPalette;
  reordering: boolean;
  onOpen: (item: PinnedItem) => void;
  onUnpin: (key: PinKey) => void;
  onMove: (key: PinKey, delta: -1 | 1) => void;
  onReorder: (on: boolean) => void;
}) {
  if (items.length === 0) return null;
  const drawn = reordering ? items.filter((item) => item.kind !== "session") : items;

  return (
    <ScrollView
      horizontal
      showsHorizontalScrollIndicator={false}
      contentContainerStyle={styles.content}
      testID="pinned-strip"
    >
      {drawn.map((item, index) => (
        <PinnedColumn
          key={item.key}
          item={item}
          palette={palette}
          reordering={reordering}
          isFirst={index === 0}
          isLast={index === drawn.length - 1}
          onOpen={onOpen}
          onUnpin={onUnpin}
          onMove={onMove}
          onReorder={onReorder}
        />
      ))}
      {reordering ? (
        <Pressable onPress={() => onReorder(false)} style={styles.done} hitSlop={8}>
          <Text style={[styles.doneLabel, { color: palette.link }]}>Done</Text>
        </Pressable>
      ) : null}
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  // paddingTop matches the ring's -4 offset (Avatar.tsx) so its top arc has
  // room instead of being clipped flush against whatever sits above the strip.
  content: { paddingHorizontal: spacing.lg, paddingTop: spacing.xs, gap: spacing.md },
  column: { width: 72, alignItems: "center", gap: spacing.xs },
  name: { fontFamily: fonts.sans, fontSize: 12, textAlign: "center" },
  moveRow: { flexDirection: "row", gap: spacing.xs },
  done: { minWidth: 44, minHeight: 44, alignItems: "center", justifyContent: "center" },
  doneLabel: { fontFamily: fonts.sansSemiBold, fontSize: 15 },
});
