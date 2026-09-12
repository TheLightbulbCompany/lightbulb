import Feather from "@expo/vector-icons/Feather";
import { linkifyMentions, type CanonicalRoomMessage, type ImageAttachment, type RoomMember } from "@lightbulb/shared";
import { useEffect, useState } from "react";
import { Image, Pressable, StyleSheet, Text, View } from "react-native";

import { colors, fonts, radii, spacing } from "../../config/theme";
import { blobToDataUri, type LocalMessageRow } from "../../lib/rooms";
import { MessageMarkdown } from "../chat/MessageMarkdown";
import { Avatar } from "./Avatar";

const AVATAR = 36;
/** Body indent under a collapsed run, so text lines up with the run above.
 *  Exported so anything drawing beneath a group lines up with its body. */
export const MESSAGE_BODY_INDENT = AVATAR + spacing.sm;

function formatTime(createdAt: string): string {
  const at = new Date(createdAt);
  if (Number.isNaN(at.getTime())) return "";
  return at.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
}

/** One attachment, fetched with the caller's authenticated reader. */
function AttachmentImage({
  roomId,
  attachment,
  downloadAttachment,
}: {
  roomId: string;
  attachment: ImageAttachment;
  downloadAttachment: (roomId: string, attachmentId: string) => Promise<Blob>;
}) {
  const [uri, setUri] = useState<string | null>(null);
  useEffect(() => {
    let cancelled = false;
    void downloadAttachment(roomId, attachment.id)
      .then(blobToDataUri)
      .then((next) => {
        if (!cancelled) setUri(next);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [roomId, attachment.id, downloadAttachment]);
  return uri ? (
    <Image
      source={{ uri }}
      style={styles.image}
      resizeMode="cover"
      accessibilityLabel={attachment.fileName}
    />
  ) : (
    <View style={[styles.image, styles.imagePending]} />
  );
}

/**
 * The applied-reactions row: one chip per emoji with a nonzero reactor count,
 * drawn under the message body. Mirrors web's `RoomMessageItem` `appliedReactions`
 * block — same wording, same "state is a border and a tint, never a loud fill"
 * rule `MessageActionsSheet.styles.reactionActive` already follows.
 */
function MessageReactions({
  reactions,
  currentUserId,
  onToggle,
}: {
  reactions: [string, string[]][];
  currentUserId: string;
  onToggle: (emoji: string, active: boolean) => void;
}) {
  return (
    <View style={styles.reactionsRow} accessibilityLabel="Message reactions">
      {reactions.map(([emoji, users]) => {
        const selected = users.includes(currentUserId);
        return (
          <Pressable
            key={emoji}
            onPress={() => onToggle(emoji, !selected)}
            accessibilityRole="button"
            accessibilityLabel={`${selected ? "Remove" : "Add"} ${emoji} reaction${users.length ? `, ${users.length}` : ""}`}
            accessibilityState={{ selected }}
            style={({ pressed }) => [
              styles.reactionChip,
              selected && styles.reactionChipActive,
              pressed && styles.reactionChipPressed,
            ]}
          >
            <Text style={styles.reactionEmoji}>{emoji}</Text>
            <Text style={[styles.reactionCount, selected && styles.reactionCountActive]}>
              {users.length}
            </Text>
          </Pressable>
        );
      })}
    </View>
  );
}

/**
 * One `message` timeline item, Slack's shape: avatar + author + time on the
 * first message of an author's run, body only on the rest. A long press
 * reveals the time of a collapsed message (and hands the press on to the
 * caller, which is where the actions sheet lands).
 */
export function MessageGroup({
  message,
  collapsed,
  replyCount,
  currentUserId,
  onLongPress,
  onOpenThread,
  onToggleReaction,
  reactionError = false,
  downloadAttachment,
  members = [],
}: {
  message: CanonicalRoomMessage;
  collapsed: boolean;
  replyCount: number;
  /** Marks the viewer's own reaction chip as selected and tints a message that mentions them. */
  currentUserId: string;
  onLongPress?: () => void;
  onOpenThread?: () => void;
  /** Omitted (e.g. for an in-flight local row) hides the reactions row entirely. */
  onToggleReaction?: (emoji: string, active: boolean) => void;
  /** True when this message's last reaction toggle failed. */
  reactionError?: boolean;
  downloadAttachment: (roomId: string, attachmentId: string) => Promise<Blob>;
  members?: readonly RoomMember[];
}) {
  const [timeShown, setTimeShown] = useState(false);
  const time = formatTime(message.createdAt);
  const appliedReactions = Object.entries(message.reactions ?? {}).filter(
    ([, users]) => users.length > 0,
  );
  // Web's `.room-message-mentions-me` (RoomPanel.tsx): the whole row tints
  // when the message's structured mentions name the viewer.
  const mentionsMe = message.mentions.some(
    (mention) => mention.kind === "human" && mention.id === currentUserId,
  );
  return (
    <Pressable
      testID="message-group"
      style={[collapsed ? styles.collapsed : styles.group, mentionsMe && styles.mentionsMe]}
      onLongPress={() => {
        setTimeShown(true);
        onLongPress?.();
      }}
      delayLongPress={300}
    >
      {collapsed ? (
        timeShown && time ? <Text style={[styles.time, styles.collapsedTime]}>{time}</Text> : null
      ) : (
        <View style={styles.head}>
          <Avatar name={message.author.displayName} kind={message.author.kind} size={AVATAR} />
          <Text style={styles.author} numberOfLines={1}>
            {message.author.displayName}
          </Text>
          {time ? <Text style={styles.time}>{time}</Text> : null}
        </View>
      )}
      <View style={collapsed ? undefined : styles.body}>
        {message.deletedAt ? (
          <Text style={styles.deleted}>Message deleted</Text>
        ) : (
          <MessageMarkdown text={linkifyMentions(message.text, message.mentions, members)} />
        )}
        {!message.deletedAt && onToggleReaction && appliedReactions.length > 0 ? (
          <MessageReactions
            reactions={appliedReactions}
            currentUserId={currentUserId}
            onToggle={onToggleReaction}
          />
        ) : null}
        {reactionError ? (
          <Text accessibilityRole="alert" style={styles.reactionError}>
            Could not update reaction. Try again.
          </Text>
        ) : null}
        {(message.attachments ?? []).map((attachment) => (
          <AttachmentImage
            key={attachment.id}
            roomId={message.roomId}
            attachment={attachment}
            downloadAttachment={downloadAttachment}
          />
        ))}
        {replyCount > 0 ? (
          <Pressable onPress={onOpenThread} accessibilityRole="button" hitSlop={6}>
            <Text style={styles.replies}>
              {replyCount} {replyCount === 1 ? "reply" : "replies"}
            </Text>
          </Pressable>
        ) : null}
      </View>
    </Pressable>
  );
}

/**
 * The messages that exist only on this device — in flight, or failed and
 * waiting on a tap. Drawn through the same `MessageGroup` as everything else so
 * the user's own text survives a failure (the composer has already cleared).
 */
export function LocalMessageRows({
  rows,
  onRetry,
  downloadAttachment,
  members = [],
  currentUserId,
}: {
  rows: readonly LocalMessageRow[];
  onRetry: (clientMessageId: string) => void;
  downloadAttachment: (roomId: string, attachmentId: string) => Promise<Blob>;
  members?: readonly RoomMember[];
  currentUserId: string;
}) {
  return (
    <>
      {rows.map(({ message, unsent }) => (
        <View key={message.messageId}>
          <MessageGroup
            message={message}
            collapsed={false}
            replyCount={0}
            downloadAttachment={downloadAttachment}
            members={members}
            currentUserId={currentUserId}
          />
          {unsent ? (
            <Pressable
              style={styles.unsent}
              onPress={() => onRetry(message.messageId)}
              accessibilityRole="button"
            >
              <Feather name="alert-circle" size={12} color={colors.errorText} />
              <Text style={styles.unsentText}>Not sent · tap to retry</Text>
            </Pressable>
          ) : null}
        </View>
      ))}
    </>
  );
}

const styles = StyleSheet.create({
  group: { marginTop: spacing.lg },
  collapsed: { marginTop: spacing.xs, paddingLeft: MESSAGE_BODY_INDENT },
  // Web's `.room-message-mentions-me`.
  mentionsMe: { backgroundColor: colors.mentionRowBg, borderRadius: radii.sm },
  head: { flexDirection: "row", alignItems: "center", gap: spacing.sm },
  author: { fontFamily: fonts.sansSemiBold, fontSize: 15, lineHeight: 22, color: colors.ink },
  time: { fontFamily: fonts.sans, fontSize: 12, lineHeight: 16, color: colors.mute },
  collapsedTime: { marginBottom: 2 },
  body: { paddingLeft: MESSAGE_BODY_INDENT, marginTop: 2 },
  deleted: { fontFamily: fonts.sans, fontSize: 14, lineHeight: 20, color: colors.mute, fontStyle: "italic" },
  reactionsRow: { flexDirection: "row", flexWrap: "wrap", gap: spacing.xs, marginTop: spacing.xs },
  reactionChip: {
    flexDirection: "row",
    alignItems: "center",
    gap: 4,
    minHeight: 44,
    paddingHorizontal: spacing.md,
    borderRadius: radii.xl,
    borderWidth: 1,
    borderColor: colors.edge,
    backgroundColor: colors.chip,
  },
  // The manual's rule: state is a border and a tint, never a loud fill.
  reactionChipActive: { backgroundColor: colors.forestTint, borderColor: colors.forest },
  reactionChipPressed: { backgroundColor: colors.rowPressed },
  reactionEmoji: { fontSize: 14, lineHeight: 18 },
  reactionCount: { fontFamily: fonts.sansMedium, fontSize: 13, lineHeight: 18, color: colors.inkSoft },
  reactionCountActive: { color: colors.forest },
  reactionError: { fontFamily: fonts.sans, fontSize: 13, lineHeight: 18, color: colors.errorText, marginTop: spacing.xs },
  replies: { fontFamily: fonts.sansMedium, fontSize: 13, lineHeight: 18, color: colors.forest, marginTop: spacing.xs },
  unsent: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.xs,
    marginTop: spacing.xs,
    paddingLeft: MESSAGE_BODY_INDENT,
  },
  unsentText: { fontFamily: fonts.sansMedium, fontSize: 12, lineHeight: 16, color: colors.errorText },
  image: { width: 220, height: 220, borderRadius: radii.md, marginTop: spacing.sm },
  imagePending: { backgroundColor: colors.skeleton },
});
