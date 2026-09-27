import {
  roomDisplayName,
  roomPreview,
  sessionLabel,
  sessionPreview,
  sessionRingState,
  sessionStatus,
  type AvatarRing,
  type LocalSession,
  type RoomSummary,
} from "@lightbulb/shared";
import { Link } from "expo-router";
import { SymbolView, type SFSymbol } from "expo-symbols";
import { useRef } from "react";
import { Pressable, StyleSheet, Text, View, type ViewStyle } from "react-native";
import ReanimatedSwipeable, {
  type SwipeableMethods,
} from "react-native-gesture-handler/ReanimatedSwipeable";

import { Avatar } from "../room/Avatar";
import { fonts, radii, spacing, type ChatPalette } from "../../config/theme";
import type { RowParticipant } from "../../lib/chats";
import { hapticSelection } from "../../lib/haptics";
import { relativeTime } from "../../lib/relativeTime";
import { ListRow } from "../ui/ListRow";

export type ChatRowAction = "markRead" | "markUnread" | "mute" | "star" | "copyLink" | "pin" | "archive";

/**
 * The channel leading tile — the `#` glyph column shared by ChatRow (52pt) and
 * PinnedStrip (Task 8, 56pt). Fixed-size tile: the `#` never scales with the
 * reader's text size, or it spills out of it.
 */
export function ChannelTile({ size = 52, palette }: { size?: number; palette: ChatPalette }) {
  return (
    <View
      style={[
        styles.channelTile,
        { width: size, height: size, borderRadius: size > 32 ? radii.lg : radii.md, backgroundColor: palette.selected },
      ]}
    >
      <Text style={[styles.channelHash, { color: palette.fgSoft }]} maxFontSizeMultiplier={1}>
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
 * anatomy as today's `RoomRow.tsx` (module-level `openRow`, iOS Mail-style
 * swipe buttons, a `Link.Menu` context menu). Deleted once Task 10 removes
 * `RoomRow.tsx` — this is its Chats-tab successor.
 */
export function RoomChatRow({
  room,
  others,
  viewerId,
  ring,
  live,
  hasDraft,
  pinned,
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
  pinned: boolean;
  palette: ChatPalette;
  onAction: (action: ChatRowAction) => void;
}) {
  const self = useRef<SwipeableMethods>(null);
  const name = roomDisplayName(room, others);
  const unread = room.unreadCount > 0 && !room.muted;
  const readAction: ChatRowAction = unread ? "markRead" : "markUnread";
  const readLabel = unread ? "Mark read" : "Mark unread";
  const muteLabel = room.muted ? "Unmute conversation" : "Mute conversation";
  const starLabel = room.starred ? "Unstar conversation" : "Star conversation";
  const pinLabel = pinned ? "Unpin conversation" : "Pin conversation";
  const label = `${name}${unread ? `, ${room.unreadCount} unread` : ""}${room.muted ? ", muted" : ""}${hasDraft ? ", draft" : ""}${live ? `, ${live}` : ""}`;
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
          icon={unread ? "envelope.open.fill" : "envelope.badge.fill"}
          label={unread ? "Read" : "Unread"}
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
          background={palette.fgSoft}
          foreground={palette.surface}
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
            preview={live ?? roomPreview(room, viewerId)}
            previewTone={live ? "live" : "normal"}
            unread={unread}
            testID={`chat-row-room-${room.roomId}`}
            accessibilityLabel={label}
            accessibilityActions={[
              { name: readAction, label: readLabel },
              { name: "mute", label: muteLabel },
              { name: "star", label: starLabel },
              { name: "pin", label: pinLabel },
              { name: "copyLink", label: "Copy link" },
            ]}
            onAccessibilityAction={(event) => onAction(event.nativeEvent.actionName as ChatRowAction)}
          />
        </Link.Trigger>
        <Link.Preview />
        <Link.Menu>
          <Link.MenuAction icon={pinned ? "pin.slash" : "pin"} onPress={() => onAction("pin")}>
            {pinned ? "Unpin" : "Pin"}
          </Link.MenuAction>
          <Link.MenuAction icon={room.starred ? "star.slash" : "star"} onPress={() => onAction("star")}>
            {room.starred ? "Unstar" : "Star"}
          </Link.MenuAction>
          <Link.MenuAction icon={room.muted ? "bell" : "bell.slash"} onPress={() => onAction("mute")}>
            {room.muted ? "Unmute" : "Mute"}
          </Link.MenuAction>
          <Link.MenuAction icon="envelope.badge" onPress={() => onAction("markUnread")}>
            Mark unread
          </Link.MenuAction>
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
  onAction: (action: Extract<ChatRowAction, "markRead" | "archive">) => void;
}) {
  const self = useRef<SwipeableMethods>(null);
  const busy = session.status === "busy";
  const label = sessionLabel(session);
  const preview = busy
    ? `${own ? "" : `${ownerName} · `}working…`
    : `${own ? "" : `${ownerName}: `}${sessionPreview(session)}`;
  const rowLabel = `${label}, ${sessionStatus[session.status]}${unread ? ", new output" : ""}${own ? "" : `, ${ownerName}`}`;
  const commit = (swipe: SwipeableMethods, action: Extract<ChatRowAction, "markRead" | "archive">) => {
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
        <SwipeAction
          icon="archivebox.fill"
          label="Archive"
          accessibilityLabel="Archive session"
          background={palette.fgSoft}
          foreground={palette.surface}
          onPress={() => commit(swipe, "archive")}
        />
      )}
    >
      <Link href={`/(app)/session/${session.sessionId}`} asChild>
        <Link.Trigger>
          <ListRow
            palette={palette}
            leading={
              <Avatar
                name={label}
                kind="agent"
                vendor={session.provider}
                ring={sessionRingState(session)}
                size={52}
                palette={palette}
              />
            }
            title={label}
            time={relativeTime(Date.parse(session.lastEventAt ?? ""))}
            preview={preview}
            previewTone={busy ? "live" : "normal"}
            unread={unread}
            testID={`chat-row-session-${session.sessionId}`}
            accessibilityLabel={rowLabel}
            accessibilityActions={[
              ...(unread ? [{ name: "markRead", label: "Mark read" }] : []),
              { name: "archive", label: "Archive session" },
            ]}
            onAccessibilityAction={(event) =>
              onAction(event.nativeEvent.actionName as Extract<ChatRowAction, "markRead" | "archive">)
            }
          />
        </Link.Trigger>
        <Link.Menu>
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
  channelHash: { fontFamily: fonts.sansSemiBold, fontSize: 22 },
  groupLead: { width: 52, height: 52 },
  groupSecond: { position: "absolute", left: 16, top: 16 },
  accessory: { fontFamily: fonts.sans, fontSize: 13 },
  swipeAction: { width: 80, alignItems: "center", justifyContent: "center", gap: spacing.xs },
  swipePressed: { opacity: 0.8 },
  swipeLabel: { fontFamily: fonts.sansMedium, fontSize: 13, lineHeight: 16 },
});
