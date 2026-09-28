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

import { fonts, radii, spacing, type ChatPalette } from "../../config/theme";
import type { PinKey, PinnedItem } from "../../lib/chats";
import { Avatar } from "../room/Avatar";
import { SessionToolIcon } from "../session/SessionToolIcon";
import { ChannelTile } from "./ChatRow";

const TILE_SIZE = 56;
const RING_WORD: Record<AvatarRing, string> = {
  running: "working",
  attention: "needs attention",
  paused: "paused",
};
/**
 * What a session card says under its title: the same line, in the same colour,
 * as the row for that session further down the list. The card stays quiet --
 * three of them outlined in the ring's green read as three alarms.
 */
const SESSION_STATE: Record<AvatarRing, { line: string; tone: "link" | "alert" | "fgMute" }> = {
  running: { line: "working…", tone: "link" },
  attention: { line: "needs you", tone: "alert" },
  paused: { line: "paused", tone: "fgMute" },
};

/** An item's ring, or null (rooms other than a solo agent DM never ring). */
function pinRing(item: PinnedItem): AvatarRing | null {
  if (item.kind === "agent") return item.ring;
  if (item.kind === "session") return sessionRingState(item.session);
  return null;
}

/**
 * A running session in the strip: its title, readable, and what it is doing.
 *
 * Not an avatar tile. A person or an agent has a face to recognise; every
 * session has the same one (its tool's logo), so a row of tiles was a row of
 * identical marks told apart by ten characters of label. The title is the
 * identity here, so it gets the room: two lines, with the state under it.
 */
function SessionCard({
  item,
  ring,
  label,
  palette,
  onOpen,
}: {
  item: Extract<PinnedItem, { kind: "session" }>;
  ring: AvatarRing | null;
  label: string;
  palette: ChatPalette;
  onOpen: (item: PinnedItem) => void;
}) {
  const state = ring ? SESSION_STATE[ring] : null;
  // Waiting on its owner: that is "you" on the reader's own session only.
  const line = state && ring === "attention" && !item.own ? "waiting for input" : state?.line;
  return (
    <Pressable
      onPress={() => onOpen(item)}
      accessibilityRole="button"
      accessibilityLabel={label}
      style={({ pressed }) => [
        styles.card,
        { backgroundColor: pressed ? palette.raised : palette.surface, borderColor: palette.edge },
      ]}
    >
      <Text style={[styles.cardTitle, { color: palette.fg }]} numberOfLines={2}>
        {item.name}
      </Text>
      <View style={styles.cardState}>
        <SessionToolIcon provider={item.session.provider} size={12} tint={palette.fgSoft} />
        {state ? <Text style={[styles.cardStateLine, { color: palette[item.own || ring !== "attention" ? state.tone : "fgMute"] }]}>{line}</Text> : null}
      </View>
    </Pressable>
  );
}

function PinnedTile({ item, ring, palette }: { item: Exclude<PinnedItem, { kind: "session" }>; ring: AvatarRing | null; palette: ChatPalette }) {
  if (item.kind === "agent") {
    return <Avatar name={item.name} kind="agent" ring={ring} size={TILE_SIZE} palette={palette} />;
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
  // A session card shows only its title; the tool is the logo, so say it aloud.
  const who = item.kind === "session" ? `${sessionToolName(item.session.provider)} · ${item.name}` : item.name;
  const label = `${who}${ring ? `, ${RING_WORD[ring]}` : ""}`;
  if (item.kind === "session") {
    return <SessionCard item={item} ring={ring} label={label} palette={palette} onOpen={onOpen} />;
  }

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
        onLongPress={openSheet}
        disabled={reordering}
        accessibilityRole="button"
        accessibilityLabel={label}
        accessibilityActions={[{ name: "unpin", label: "Unpin" }, { name: "reorder", label: "Reorder pins" }]}
        onAccessibilityAction={(event) => {
          if (event.nativeEvent.actionName === "unpin") onUnpin(item.key as PinKey);
          if (event.nativeEvent.actionName === "reorder") onReorder(true);
        }}
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
      ) : null}
      {/* Under the arrows too: a tile shows one letter, and Friday and Forge share theirs. */}
      <Text style={[styles.name, { color: palette.fgSoft }]} numberOfLines={1}>
        {item.name}
      </Text>
    </View>
  );
}

/**
 * The pinned strip atop Chats: live sessions (when the switch is on) as cards,
 * then agents and rooms as 72pt columns. Long-press an agent/room pin for Unpin or
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
  // As tall as a column (tile, gap, name), so the strip keeps one baseline.
  card: {
    width: 144,
    height: TILE_SIZE + spacing.xs + 16,
    borderRadius: radii.lg,
    borderWidth: StyleSheet.hairlineWidth,
    paddingHorizontal: spacing.md,
    paddingVertical: spacing.sm,
    justifyContent: "space-between",
  },
  cardTitle: { fontFamily: fonts.sansSemiBold, fontSize: 13, lineHeight: 17 },
  cardState: { flexDirection: "row", alignItems: "center", gap: spacing.xs },
  cardStateLine: { fontFamily: fonts.sans, fontSize: 13, lineHeight: 16 },
  name: { fontFamily: fonts.sans, fontSize: 12, textAlign: "center" },
  moveRow: { flexDirection: "row", gap: spacing.xs },
  done: { minWidth: 44, minHeight: 44, alignItems: "center", justifyContent: "center" },
  doneLabel: { fontFamily: fonts.sansSemiBold, fontSize: 15 },
});
