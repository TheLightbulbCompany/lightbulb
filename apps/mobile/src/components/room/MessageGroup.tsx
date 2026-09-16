import { SymbolView } from "expo-symbols";
import { linkifyMentions, type CanonicalRoomMessage, type MessageAttachment, type RoomMember } from "@lightbulb/shared";
import { Image } from "expo-image";
import { useEffect, useState } from "react";
import { Pressable, StyleSheet, Text, View } from "react-native";

import { colors, fonts, radii, spacing } from "../../config/theme";
import { hapticImpact, hapticSelection } from "../../lib/haptics";
import { blobToDataUri, type LocalMessageRow } from "../../lib/rooms";
import { MessageMarkdown } from "../chat/MessageMarkdown";
import { AttachmentViewerModal } from "./AttachmentViewerModal";
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

/**
 * One attachment thumbnail: an image preview (fetched with the caller's
 * authenticated reader), or — for anything else the schema someday carries
 * (PDF, video, ...) — a compact file row. Either way, tapping it opens the
 * full-screen viewer at this attachment. The `Pressable` here claims only its
 * own `onPress`, so it doesn't interfere with the message group's own
 * `onLongPress` (MessageActionsSheet) above it.
 */
function AttachmentThumbnail({
  roomId,
  attachment,
  downloadAttachment,
  onPress,
}: {
  roomId: string;
  attachment: MessageAttachment;
  downloadAttachment: (roomId: string, attachmentId: string) => Promise<Blob>;
  onPress: () => void;
}) {
  const isImage = attachment.contentType.startsWith("image/");
  const [uri, setUri] = useState<string | null>(null);
  useEffect(() => {
    if (!isImage) return;
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
  }, [isImage, roomId, attachment.id, downloadAttachment]);

  const label = `Open ${attachment.fileName}`;
  if (!isImage) {
    return (
      <Pressable
        onPress={onPress}
        accessibilityRole="button"
        accessibilityLabel={label}
        style={({ pressed }) => [styles.fileRow, pressed && styles.fileRowPressed]}
      >
        <SymbolView accessibilityElementsHidden name="doc" size={16} tintColor={colors.mute} />
        <Text style={styles.fileRowName} numberOfLines={1}>
          {attachment.fileName}
        </Text>
      </Pressable>
    );
  }
  return (
    <Pressable onPress={onPress} accessibilityRole="button" accessibilityLabel={label}>
      {uri ? (
        <Image source={{ uri }} style={styles.image} contentFit="cover" cachePolicy="memory" />
      ) : (
        <View style={[styles.image, styles.imagePending]} />
      )}
    </Pressable>
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
            onPress={() => {
              hapticSelection();
              onToggle(emoji, !selected);
            }}
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
  const [viewerIndex, setViewerIndex] = useState<number | null>(null);
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
    <>
      <Pressable
        testID="message-group"
        style={[collapsed ? styles.collapsed : styles.group, mentionsMe && styles.mentionsMe]}
        onLongPress={() => {
          setTimeShown(true);
          // Fired here, same frame as the caller opening the actions sheet —
          // not after it mounts, so the tap and the buzz land together.
          hapticImpact();
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
          {(message.attachments ?? []).map((attachment, attachmentIndex) => (
            <AttachmentThumbnail
              key={attachment.id}
              roomId={message.roomId}
              attachment={attachment}
              downloadAttachment={downloadAttachment}
              onPress={() => setViewerIndex(attachmentIndex)}
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
      {viewerIndex !== null && message.attachments && message.attachments.length > 0 ? (
        <AttachmentViewerModal
          attachments={message.attachments}
          initialIndex={viewerIndex}
          roomId={message.roomId}
          downloadAttachment={downloadAttachment}
          onClose={() => setViewerIndex(null)}
        />
      ) : null}
    </>
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
              <SymbolView accessibilityElementsHidden name="exclamationmark.circle" size={12} tintColor={colors.errorText} />
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
  // The name gives way (truncates) so the time stays on screen at large text sizes.
  author: { flexShrink: 1, fontFamily: fonts.sansSemiBold, fontSize: 15, lineHeight: 22, color: colors.ink },
  time: { flexShrink: 0, fontFamily: fonts.sans, fontSize: 12, lineHeight: 16, color: colors.mute },
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
  // Non-image attachment thumbnail (PDF, video, ...): a compact tappable row,
  // same chip language as AttachmentChips' pending-upload row.
  fileRow: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.xs,
    minHeight: 44,
    marginTop: spacing.sm,
    paddingHorizontal: spacing.md,
    borderRadius: radii.lg,
    backgroundColor: colors.chip,
    alignSelf: "flex-start",
    maxWidth: "100%",
  },
  fileRowPressed: { backgroundColor: colors.rowPressed },
  fileRowName: { fontFamily: fonts.sans, fontSize: 13, lineHeight: 18, color: colors.ink, flexShrink: 1 },
});
