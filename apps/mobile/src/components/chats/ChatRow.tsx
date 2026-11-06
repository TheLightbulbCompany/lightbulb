import {
  roomDisplayName,
  roomPreview,
  displaySessionMentions,
  sessionLabel,
  sessionPreview,
  sessionRingState,
  sessionSignalsLine,
  sessionStateLabel,
  sessionTitle,
  type AvatarRing,
  type LocalSession,
  type RoomSummary,
} from "@lightbulb/shared";
import { Link } from "expo-router";
import { SymbolView, type SFSymbol } from "../ui/SymbolView";
import { useRef } from "react";
import { Pressable, StyleSheet, Text, View, type ViewStyle } from "react-native";
import ReanimatedSwipeable, {
  type SwipeableMethods,
} from "react-native-gesture-handler/ReanimatedSwipeable";

import { Avatar, RING_LABEL } from "../room/Avatar";
import { fonts, radii, spacing, type ChatPalette } from "../../config/theme";
import type { RowParticipant } from "../../lib/chats";
import { hapticSelection } from "../../lib/haptics";
import { relativeTime } from "../../lib/relativeTime";
import { ListRow } from "../ui/ListRow";

export type ChatRowAction = "markRead" | "markUnread" | "mute" | "star" | "copyLink" | "archive" | "rename";

/**
 * The channel leading tile: the `#` glyph column. Fixed-size tile: the `#`
 * never scales with the reader's text size, or it spills out of it.
 */
export function ChannelTile({ size = 52, palette }: { size?: number; palette: ChatPalette }) {
  return (
    <View
      style={[
        styles.channelTile,
        { width: size, height: size, borderRadius: size > 32 ? radii.lg : radii.md, backgroundColor: palette.selected },
      ]}
    >
      <Text style={[styles.channelHash, { color: palette.fgSoft, fontSize: Math.round(size * 0.42) }]} maxFontSizeMultiplier={1}>
        #
      </Text>
    </View>
  );
}

/** The one row whose swipe is showing, across every room AND session row in the list. */
let openRow: SwipeableMethods | null = null;

function SwipeAction({
  icon,
  label,
  accessibilityLabel,
  background,
  foreground,
  onPress,
}: {
  icon: SFSymbol;
  label: string;
  accessibilityLabel: string;
  background: string;
  foreground: string;
  onPress: () => void;
}) {
  return (
    <Pressable
      onPress={onPress}
      accessibilityRole="button"
      accessibilityLabel={accessibilityLabel}
      accessibilityElementsHidden
      importantForAccessibility="no-hide-descendants"
      style={({ pressed }): ViewStyle[] => [
        styles.swipeAction,
        { backgroundColor: background },
        pressed ? styles.swipePressed : {},
      ]}
    >
      <SymbolView accessibilityElementsHidden name={icon} size={20} tintColor={foreground} />
      <Text style={[styles.swipeLabel, { color: foreground }]}>{label}</Text>
    </Pressable>
  );
}

function RoomLeading({
  room,
  others,
  ring,
  palette,
}: {
  room: RoomSummary;
  others: readonly RowParticipant[];
  ring: AvatarRing | null;
  palette: ChatPalette;
}) {
  if (room.kind === "channel") return <ChannelTile palette={palette} />;
  if (room.kind === "group-dm") {
    return (
      <View style={styles.groupLead}>
        <Avatar name={others[0]?.displayName ?? "?"} kind={others[0]?.kind ?? "human"} size={36} palette={palette} />
        <View style={styles.groupSecond}>
          <Avatar name={others[1]?.displayName ?? "?"} kind={others[1]?.kind ?? "human"} size={36} palette={palette} />
        </View>
      </View>
    );
  }
  const other = others[0];
  return (
    <Avatar
      name={other?.displayName ?? "?"}
      kind={other?.kind ?? "human"}
      ring={ring}
      online={other?.online}
      sharing={other?.sharing}
      showPresence
      size={52}
      palette={palette}
    />
  );
}

/**
 * One Chats-list room row: leading tile/avatar(s), title with live state
 * accessories, a live-or-last-message preview, and the same swipe/menu
 * anatomy as the former `RoomRow.tsx` (module-level `openRow`, iOS Mail-style
 * swipe buttons, a `Link.Menu` context menu) — this is its Chats-tab
 * successor; `RoomRow.tsx` was deleted in Task 10.
 */
export function RoomChatRow({
  room,
  others,
  viewerId,
  ring,
  live,
  hasDraft,
  sessions = [],
  palette,
  onAction,
}: {
  room: RoomSummary;
  others: readonly RowParticipant[];
  viewerId: string | null | undefined;
  /** The DM agent's ring (null for channels/groups/humans). */
  ring: AvatarRing | null;
  /** useRoomActivity's liveLine: "Ann is typing…" / "working…" / "CEO is working…". */
  live: string | null;
  hasDraft: boolean;
  sessions?: readonly LocalSession[];
  palette: ChatPalette;
  onAction: (action: ChatRowAction) => void;
}) {
  const self = useRef<SwipeableMethods>(null);
  const name = roomDisplayName(room, others);
  const hasUnread = room.unreadCount > 0;
  // The dot stays muted-suppressed (visual only); the read/unread action must
  // reflect the real unread state, or a muted-and-unread room offers "Mark
  // unread" on a room that is already unread.
  const unread = hasUnread && !room.muted;
  const readAction: ChatRowAction = hasUnread ? "markRead" : "markUnread";
  const readLabel = hasUnread ? "Mark read" : "Mark unread";
  const muteLabel = room.muted ? "Unmute" : "Mute";
  const starLabel = room.starred ? "Unstar" : "Star";
  // The row's Pressable is one VoiceOver focus stop, so a DM counterpart's
  // sharing badge on the Avatar (see RoomLeading) is never independently
  // reached — it has to ride the row's own label, same as the former RoomRow.
  const sharingSuffix = room.kind === "dm" && others[0]?.sharing ? ", sharing a terminal session" : "";
  // The Avatar ring (working / paused / needs attention) is likewise unreachable
  // on its own; skip it when the live hint already says what the agent is doing.
  const ringSuffix = ring && !live ? `, ${RING_LABEL[ring].toLowerCase()}` : "";
  const label = `${name}${ringSuffix}${unread ? `, ${room.unreadCount} unread` : ""}${room.muted ? ", muted" : ""}${hasDraft ? ", draft" : ""}${live ? `, ${live}` : ""}${sharingSuffix}`;
  const commit = (swipe: SwipeableMethods, action: ChatRowAction) => {
    swipe.close();
    hapticSelection();
    onAction(action);
  };
  const titleAccessory = (
    <>
      {hasDraft ? <Text style={[styles.accessory, { color: palette.alert }]}>Draft</Text> : null}
      {room.shared ? <Text style={[styles.accessory, { color: palette.fgMute }]}>Shared</Text> : null}
      {room.muted ? (
        <SymbolView accessibilityElementsHidden name="bell.slash" size={13} tintColor={palette.fgMute} />
      ) : null}
    </>
  );
  return (
    <ReanimatedSwipeable
      overshootLeft={false}
      overshootRight={false}
      ref={self}
      onSwipeableWillOpen={() => {
        if (openRow && openRow !== self.current) openRow.close();
        openRow = self.current;
      }}
      onSwipeableWillClose={() => {
        if (openRow === self.current) openRow = null;
      }}
      childrenContainerStyle={{ backgroundColor: palette.surface }}
      renderLeftActions={(_progress, _translation, swipe) => (
        <SwipeAction
          icon={hasUnread ? "envelope.open.fill" : "envelope.badge.fill"}
          label={hasUnread ? "Read" : "Unread"}
          accessibilityLabel={readLabel}
          background={palette.link}
          foreground={palette.onLink}
          onPress={() => commit(swipe, readAction)}
        />
      )}
      renderRightActions={(_progress, _translation, swipe) => (
        <SwipeAction
          icon={room.muted ? "bell.fill" : "bell.slash.fill"}
          label={room.muted ? "Unmute" : "Mute"}
          accessibilityLabel={muteLabel}
          // The quiet action: a step off the row's own surface, not a fill.
          // In dark mode the soft ink it used is pale, and it drew a bright slab.
          background={palette.selected}
          foreground={palette.fg}
          onPress={() => commit(swipe, "mute")}
        />
      )}
    >
      <Link href={`/(app)/room/${room.roomId}`} asChild>
        <Link.Trigger>
          <ListRow
            palette={palette}
            leading={<RoomLeading room={room} others={others} ring={ring} palette={palette} />}
            title={name}
            titleAccessory={titleAccessory}
            time={relativeTime(Date.parse(room.lastMessageAt ?? ""))}
            preview={live ?? displaySessionMentions(roomPreview(room, viewerId), undefined, sessions)}
            previewTone={live ? "live" : "normal"}
            unread={unread}
            testID={`chat-row-room-${room.roomId}`}
            accessibilityLabel={label}
            accessibilityActions={[
              { name: readAction, label: readLabel },
              { name: "mute", label: muteLabel },
              { name: "star", label: starLabel },
              { name: "copyLink", label: "Copy link" },
            ]}
            onAccessibilityAction={(event) => onAction(event.nativeEvent.actionName as ChatRowAction)}
          />
        </Link.Trigger>
        <Link.Preview />
        <Link.Menu>
          <Link.MenuAction icon={room.starred ? "star.slash" : "star"} onPress={() => onAction("star")}>
            {room.starred ? "Unstar" : "Star"}
          </Link.MenuAction>
          <Link.MenuAction icon={room.muted ? "bell" : "bell.slash"} onPress={() => onAction("mute")}>
            {room.muted ? "Unmute" : "Mute"}
          </Link.MenuAction>
          {!hasUnread ? (
            <Link.MenuAction icon="envelope.badge" onPress={() => onAction("markUnread")}>
              Mark unread
            </Link.MenuAction>
          ) : null}
          <Link.MenuAction icon="link" onPress={() => onAction("copyLink")}>
            Copy link
          </Link.MenuAction>
        </Link.Menu>
      </Link>
    </ReanimatedSwipeable>
  );
}

/**
 * One Chats-list session row: the tool's vendor mark as the leading avatar,
 * a live "working…" preview, and a two-action swipe/menu (mark read, archive)
 * — no `Link.Preview`, since the session screen has no read-only peek mode.
 */
export function SessionChatRow({
  session,
  ownerName,
  own,
  unread,
  palette,
  onAction,
}: {
  session: LocalSession;
  ownerName: string;
  own: boolean;
  unread: boolean;
  palette: ChatPalette;
  onAction: (action: Extract<ChatRowAction, "markRead" | "archive" | "rename">) => void;
}) {
  const self = useRef<SwipeableMethods>(null);
  const busy = session.status === "busy";
  // The vendor logo already says which tool this is, so the visible title is
  // just the session's own title; VoiceOver keeps the tool name (`sessionLabel`)
  // since it has no logo to read.
  const title = sessionTitle(session);
  const voiceOverLabel = sessionLabel(session);
  // Running subagents and the branch's PR (spec 2026-10-26 (f) gaps 16, 15), after what it says.
  const signals = session.status === "ended" || session.status === "offline" ? null : sessionSignalsLine(session);
  const preview = (busy
    ? `${own ? "" : `${ownerName} · `}working…`
    : `${own ? "" : `${ownerName}: `}${sessionPreview(session)}`) + (signals ? ` · ${signals}` : "");
  const rowLabel = `${voiceOverLabel}, ${sessionStateLabel(session)}${signals ? `, ${signals}` : ""}${unread ? ", new output" : ""}${own ? "" : `, ${ownerName}`}`;
  const commit = (swipe: SwipeableMethods, action: Extract<ChatRowAction, "markRead" | "archive" | "rename">) => {
    swipe.close();
    hapticSelection();
    onAction(action);
  };
  return (
    <ReanimatedSwipeable
      overshootLeft={false}
      overshootRight={false}
      ref={self}
      onSwipeableWillOpen={() => {
        if (openRow && openRow !== self.current) openRow.close();
        openRow = self.current;
      }}
      onSwipeableWillClose={() => {
        if (openRow === self.current) openRow = null;
      }}
      childrenContainerStyle={{ backgroundColor: palette.surface }}
      renderLeftActions={
        unread
          ? (_progress, _translation, swipe) => (
              <SwipeAction
                icon="envelope.open.fill"
                label="Read"
                accessibilityLabel="Mark read"
                background={palette.link}
                foreground={palette.onLink}
                onPress={() => commit(swipe, "markRead")}
              />
            )
          : undefined
      }
      renderRightActions={(_progress, _translation, swipe) => (
        <View style={{ flexDirection: "row" }}>
          {own && <SwipeAction icon="pencil" label="Rename" accessibilityLabel="Rename session" background={palette.selected} foreground={palette.fg} onPress={() => commit(swipe, "rename")} />}
          <SwipeAction
            icon="archivebox.fill"
            label="Archive"
            accessibilityLabel="Archive session"
            background={palette.selected}
            foreground={palette.fg}
            onPress={() => commit(swipe, "archive")}
          />
        </View>
      )}
    >
      <Link href={`/(app)/session/${session.sessionId}`} asChild>
        <Link.Trigger>
          <ListRow
            palette={palette}
            leading={
              <Avatar
                name={title}
                kind="agent"
                vendor={session.provider}
                ring={sessionRingState(session)}
                size={52}
                palette={palette}
              />
            }
            title={title}
            time={relativeTime(Date.parse(session.lastEventAt ?? ""))}
            preview={preview}
            previewTone={busy ? "live" : "normal"}
            unread={unread}
            testID={`chat-row-session-${session.sessionId}`}
            accessibilityLabel={rowLabel}
            accessibilityActions={[
              ...(unread ? [{ name: "markRead", label: "Mark read" }] : []),
              ...(own ? [{ name: "rename", label: "Rename session" }] : []),
              { name: "archive", label: "Archive session" },
            ]}
            onAccessibilityAction={(event) =>
              onAction(event.nativeEvent.actionName as Extract<ChatRowAction, "markRead" | "archive" | "rename">)
            }
          />
        </Link.Trigger>
        <Link.Menu>
          {own ? <Link.MenuAction icon="pencil" onPress={() => onAction("rename")}>Rename</Link.MenuAction> : null}
          {unread ? (
            <Link.MenuAction icon="envelope.badge" onPress={() => onAction("markRead")}>
              Mark read
            </Link.MenuAction>
          ) : null}
          <Link.MenuAction icon="archivebox" onPress={() => onAction("archive")}>
            Archive
          </Link.MenuAction>
        </Link.Menu>
      </Link>
    </ReanimatedSwipeable>
  );
}

const styles = StyleSheet.create({
  channelTile: { alignItems: "center", justifyContent: "center" },
  groupLead: { width: 52, height: 52 },
  groupSecond: { position: "absolute", left: 16, top: 16 },
  accessory: { fontFamily: fonts.sans, fontSize: 13 },
  channelHash: { fontFamily: fonts.sansSemiBold },
  swipeAction: { width: 80, alignItems: "center", justifyContent: "center", gap: spacing.xs },
  swipePressed: { opacity: 0.8 },
  swipeLabel: { fontFamily: fonts.sansMedium, fontSize: 13, lineHeight: 16 },
});
