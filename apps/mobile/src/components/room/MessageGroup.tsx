import {
  isVideoAttachmentType,
  linkifyMentions,
  linkifySessionMentions,
  unpromptedLabel,
  useSessionSendReceipt,
  type AvatarRing,
  type CanonicalRoomMessage,
  type MessageAttachment,
  type RoomMember,
  type LocalSessionCommandReceipt,
  type SessionMentionCandidate,
  type SessionSend,
} from "@lightbulb/shared";
import { Image } from "expo-image";
import { useEffect, useState } from "react";
import { Pressable, StyleSheet, Text, View } from "react-native";

import { fonts, spacing, useChatPalette, type ChatPalette } from "../../config/theme";
import { hapticImpact, hapticSelection } from "../../lib/haptics";
import { attachmentFile } from "../../lib/attachmentFile";
import type { LocalMessageRow } from "../../lib/rooms";
import { ReceiptChipText } from "../session/TranscriptRow";
import { Bubble, BubbleMeta, Chip, MetaText, AVATAR_GUTTER } from "../conversation/bubbles";
import { MessageMarkdown } from "../chat/MessageMarkdown";
import { AttachmentViewerModal } from "./AttachmentViewerModal";
import { Avatar } from "./Avatar";
import { DocumentCards } from "./DocumentCards";
import { LinkPreviews } from "./LinkPreviews";

function formatTime(createdAt: string): string {
  const at = new Date(createdAt);
  if (Number.isNaN(at.getTime())) return "";
  return at.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
}

/**
 * One attachment thumbnail: an image preview (the cached file the viewer
 * opens too) rendered inline in the bubble, or — for anything else
 * the schema someday carries (PDF, video, ...) — a `Chip` file row. Either
 * way, tapping it opens the full-screen viewer at this attachment. The
 * `Pressable`/`Chip` here claims only its own `onPress`, so it doesn't
 * interfere with the message group's own `onLongPress` (MessageActionsSheet)
 * above it.
 */
/** Tall and wide limits for a preview's box, as width over height. */
const MIN_RATIO = 0.6;
const MAX_RATIO = 4;

function AttachmentThumbnail({
  roomId,
  attachment,
  palette,
  attachmentUrl,
  onPress,
}: {
  roomId: string;
  attachment: MessageAttachment;
  palette: ChatPalette;
  attachmentUrl: (roomId: string, attachmentId: string) => Promise<{ url: string; expiresIn: number }>;
  onPress: () => void;
}) {
  const isImage = attachment.contentType.startsWith("image/");
  const isVideo = isVideoAttachmentType(attachment.contentType);
  const [uri, setUri] = useState<string | null>(null);
  // The picture's own shape once it has loaded: a wide strip of a screenshot
  // cropped to a square showed a few enlarged letters of it. The box keeps to
  // limits and the picture is drawn whole inside it, never cropped.
  const [ratio, setRatio] = useState(1);
  useEffect(() => {
    if (!isImage) return;
    let cancelled = false;
    void attachmentFile(roomId, attachment, attachmentUrl)
      .then((file) => {
        if (!cancelled) setUri(file.uri);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [isImage, roomId, attachment, attachmentUrl]);

  const label = `Open ${attachment.fileName}`;
  if (!isImage) {
    return (
      <Chip
        label={attachment.fileName}
        palette={palette}
        icon={isVideo ? "play.rectangle" : "doc"}
        onPress={onPress}
        accessibilityLabel={label}
      />
    );
  }
  return (
    <Pressable onPress={onPress} accessibilityRole="button" accessibilityLabel={label}>
      {uri ? (
        <Image
          source={{ uri }}
          style={[styles.image, { aspectRatio: ratio, backgroundColor: palette.raised }]}
          contentFit="contain"
          cachePolicy="memory"
          onLoad={({ source }) => {
            if (source.width > 0 && source.height > 0) {
              setRatio(Math.min(MAX_RATIO, Math.max(MIN_RATIO, source.width / source.height)));
            }
          }}
        />
      ) : (
        <View style={[styles.image, { backgroundColor: palette.raised }]} />
      )}
    </Pressable>
  );
}

/** One emoji's reaction chip: `${emoji} ${count}`, selected when the viewer reacted. */
function ReactionChips({
  reactions,
  currentUserId,
  palette,
  onToggle,
}: {
  reactions: [string, string[]][];
  currentUserId: string;
  palette: ChatPalette;
  onToggle: (emoji: string, active: boolean) => void;
}) {
  return (
    <>
      {reactions.map(([emoji, users]) => {
        const selected = users.includes(currentUserId);
        return (
          <Chip
            key={emoji}
            label={`${emoji} ${users.length}`}
            palette={palette}
            selected={selected}
            accessibilityLabel={`${selected ? "Remove" : "Add"} ${emoji} reaction, ${users.length}`}
            onPress={() => {
              hapticSelection();
              onToggle(emoji, !selected);
            }}
          />
        );
      })}
    </>
  );
}

/**
 * One `message` timeline item as a bubble: an avatar + name above the first
 * bubble of an author's run (others only — mine never carries either), the
 * body inside the bubble, and everything else — reactions, reply count, the
 * unprompted label, the time, the read state — hanging off it in a
 * `BubbleMeta` row. A long press on anything in the group reveals the time of
 * a collapsed message and hands the press on to the caller (the actions sheet).
 */
function SessionSendChip({ send, replyCount, receiptRefreshKey, getSessionCommand, handle }: {
  send: SessionSend;
  replyCount: number;
  receiptRefreshKey?: number;
  getSessionCommand?: (sessionId: string, requestId: string) => Promise<LocalSessionCommandReceipt>;
  handle?: string;
}) {
  const { chip } = useSessionSendReceipt(getSessionCommand, send, receiptRefreshKey ?? replyCount);
  return chip ? <ReceiptChipText chip={{ ...chip, label: handle ? `@${handle} · ${chip.label}` : chip.label }} /> : null;
}

export function MessageGroup({
  message,
  collapsed,
  replyCount,
  receiptRefreshKey,
  currentUserId,
  onLongPress,
  onOpenThread,
  onToggleReaction,
  reactionError = false,
  attachmentUrl,
  members = [],
  sessionCandidates = [],
  getSessionCommand,
  showTime = true,
  readLabel = null,
  agentRing = null,
}: {
  message: CanonicalRoomMessage;
  collapsed: boolean;
  replyCount: number;
  receiptRefreshKey?: number;
  /** Marks the viewer's own reaction chip as selected, tints a message that mentions them, and (with the author) decides `mine`. */
  currentUserId: string;
  onLongPress?: () => void;
  onOpenThread?: () => void;
  /** Omitted (e.g. for an in-flight local row) hides the reactions row entirely. */
  onToggleReaction?: (emoji: string, active: boolean) => void;
  /** True when this message's last reaction toggle failed. */
  reactionError?: boolean;
  /** A short-lived presigned S3 URL: what a thumbnail and the viewer download from, and what a video plays off. */
  attachmentUrl: (roomId: string, attachmentId: string) => Promise<{ url: string; expiresIn: number }>;
  members?: readonly RoomMember[];
  sessionCandidates?: readonly SessionMentionCandidate[];
  getSessionCommand?: (sessionId: string, requestId: string) => Promise<LocalSessionCommandReceipt>;
  /** Default true; pass false when the next bubble continues this sender's run, so the time waits for the run's last bubble (or a long press). */
  showTime?: boolean;
  /** "Sent" | "Delivered" | "Read" | "Read by N" — only ever shown on the viewer's own last message, and never on a deleted one. */
  readLabel?: string | null;
  /** Ring on this bubble's avatar (the agent is running right now). Ignored for a mine bubble, which never draws an avatar. */
  agentRing?: AvatarRing | null;
}) {
  const palette = useChatPalette();
  const [timeShown, setTimeShown] = useState(false);
  const [viewerIndex, setViewerIndex] = useState<number | null>(null);
  const mine = message.author.kind === "human" && message.author.id === currentUserId;
  const deleted = Boolean(message.deletedAt);
  // A local (not-yet-canonical) row -- `useSendDrafts` assigns it seq 0 until
  // the server echoes the real one (the same "not live yet" convention
  // `lastOwnMessageId` reads). Dimmed regardless of whether it's still
  // in flight or has failed; `LocalMessageRows` renders the difference below it.
  const pending = message.messageSeq <= 0;
  const visibleSessionSends = mine ? message.sessionSends : message.sessionSends?.filter((send) => !!send.error);
  const time = formatTime(message.createdAt);
  const showTimeNow = (showTime || timeShown) && Boolean(time);
  const appliedReactions = Object.entries(message.reactions ?? {}).filter(
    ([, users]) => users.length > 0,
  );
  // Web's `.room-message-mentions-me` (RoomPanel.tsx), now a bubble stroke
  // instead of a whole-row tint.
  const mentionsMe = message.mentions.some(
    (mention) => mention.kind === "human" && mention.id === currentUserId,
  );
  const unprompted = unpromptedLabel(message);
  const bubbleLabel = deleted
    ? "Message deleted"
    : `${mine ? "You" : message.author.displayName}: ${message.text}`;
  const markdownText = linkifySessionMentions(
    linkifyMentions(message.text, message.mentions, members), message.sessionSends,
  );

  return (
    <>
      <Pressable
        testID="message-group"
        style={[styles.group, { marginTop: collapsed ? 2 : 12 }]}
        onLongPress={() => {
          setTimeShown(true);
          // Fired here, same frame as the caller opening the actions sheet —
          // not after it mounts, so the tap and the buzz land together.
          hapticImpact();
          onLongPress?.();
        }}
        delayLongPress={300}
      >
        {!mine && !collapsed ? (
          <View style={styles.head}>
            <Avatar
              name={message.author.displayName}
              kind={message.author.kind}
              size={28}
              ring={agentRing}
              palette={palette}
            />
            <View style={styles.headCol}>
              <Text style={[styles.authorName, { color: palette.fgMute }]} numberOfLines={1}>
                {message.author.displayName}
              </Text>
              <Bubble
                testID="message-bubble"
                mine={mine}
                palette={palette}
                mentionsMe={mentionsMe}
                dimmed={pending}
                accessibilityLabel={bubbleLabel}
              >
                {deleted ? (
                  <Text style={[styles.deleted, { color: palette.fgMute }]}>Message deleted</Text>
                ) : (
                  <MessageMarkdown text={markdownText} palette={palette} />
                )}
                {!deleted ? <DocumentCards text={message.text} /> : null}
                {!deleted ? <LinkPreviews text={message.text} /> : null}
                {!deleted
                  ? (message.attachments ?? []).map((attachment, attachmentIndex) => (
                      <AttachmentThumbnail
                        key={attachment.id}
                        roomId={message.roomId}
                        attachment={attachment}
                        palette={palette}
                        attachmentUrl={attachmentUrl}
                        onPress={() => setViewerIndex(attachmentIndex)}
                      />
                    ))
                  : null}
              </Bubble>
            </View>
          </View>
        ) : (
          <View style={!mine && collapsed ? styles.collapsedIndent : undefined}>
            <Bubble
              testID="message-bubble"
              mine={mine}
              palette={palette}
              mentionsMe={mentionsMe}
              dimmed={pending}
              accessibilityLabel={bubbleLabel}
            >
              {deleted ? (
                <Text style={[styles.deleted, { color: mine ? palette.onFg : palette.fgMute, opacity: mine ? 0.6 : 1 }]}>
                  Message deleted
                </Text>
              ) : (
                <MessageMarkdown text={markdownText} palette={palette} inverse={mine} />
              )}
              {!deleted ? <DocumentCards text={message.text} /> : null}
              {!deleted ? <LinkPreviews text={message.text} /> : null}
              {!deleted
                ? (message.attachments ?? []).map((attachment, attachmentIndex) => (
                    <AttachmentThumbnail
                      key={attachment.id}
                      roomId={message.roomId}
                      attachment={attachment}
                      palette={palette}
                      attachmentUrl={attachmentUrl}
                      onPress={() => setViewerIndex(attachmentIndex)}
                    />
                  ))
                : null}
            </Bubble>
          </View>
        )}
        <BubbleMeta align={mine ? "end" : "start"} palette={palette} indent={mine ? 0 : AVATAR_GUTTER}>
          {!deleted && onToggleReaction && appliedReactions.length > 0 ? (
            <ReactionChips
              reactions={appliedReactions}
              currentUserId={currentUserId}
              palette={palette}
              onToggle={onToggleReaction}
            />
          ) : null}
          {!deleted && reactionError ? (
            <Text accessibilityRole="alert" style={[styles.reactionError, { color: palette.alert }]}>
              Could not update reaction. Try again.
            </Text>
          ) : null}
          {!deleted && !!visibleSessionSends?.length ? <View style={styles.sessionSendRow}>
            {visibleSessionSends.map((send) => <SessionSendChip key={send.sessionId} send={send} replyCount={replyCount} receiptRefreshKey={receiptRefreshKey}
              getSessionCommand={getSessionCommand} handle={sessionCandidates.find(({ session }) => session.sessionId === send.sessionId)?.handle} />)}
          </View> : null}
          {!deleted && replyCount > 0 ? (
            <Chip
              label={`${replyCount} ${replyCount === 1 ? "reply" : "replies"}`}
              palette={palette}
              onPress={onOpenThread}
            />
          ) : null}
          {unprompted ? <MetaText palette={palette}>{unprompted}</MetaText> : null}
          {showTimeNow ? <MetaText palette={palette}>{time}</MetaText> : null}
          {!deleted && readLabel ? <MetaText palette={palette}>{readLabel}</MetaText> : null}
        </BubbleMeta>
      </Pressable>
      {viewerIndex !== null && message.attachments && message.attachments.length > 0 ? (
        <AttachmentViewerModal
          attachments={message.attachments}
          initialIndex={viewerIndex}
          roomId={message.roomId}
          attachmentUrl={attachmentUrl}
          onClose={() => setViewerIndex(null)}
        />
      ) : null}
    </>
  );
}

/**
 * The messages that exist only on this device — in flight, or failed and
 * waiting on a tap. Drawn through the same `MessageGroup` as everything else so
 * the user's own text survives a failure (the composer has already cleared);
 * `MessageGroup` dims the bubble itself (`messageSeq <= 0`), and this is where
 * the two rows differ: "Sending…" while in flight, a retry button once failed.
 */
export function LocalMessageRows({
  rows,
  onRetry,
  attachmentUrl,
  members = [],
  currentUserId,
}: {
  rows: readonly LocalMessageRow[];
  onRetry: (clientMessageId: string) => void;
  attachmentUrl: (roomId: string, attachmentId: string) => Promise<{ url: string; expiresIn: number }>;
  members?: readonly RoomMember[];
  currentUserId: string;
}) {
  const palette = useChatPalette();
  return (
    <>
      {rows.map(({ message, unsent }) => (
        <View key={message.messageId}>
          <MessageGroup
            message={message}
            collapsed={false}
            replyCount={0}
              attachmentUrl={attachmentUrl}
            members={members}
            currentUserId={currentUserId}
          />
          <BubbleMeta align="end" palette={palette}>
            {unsent ? (
              <MetaText
                palette={palette}
                tone="alert"
                onPress={() => onRetry(message.messageId)}
                accessibilityLabel="Not sent, tap to retry"
              >
                Not sent · tap to retry
              </MetaText>
            ) : (
              <MetaText palette={palette}>Sending…</MetaText>
            )}
          </BubbleMeta>
        </View>
      ))}
    </>
  );
}

const styles = StyleSheet.create({
  group: { flexDirection: "column" },
  head: { flexDirection: "row", alignItems: "flex-start", gap: spacing.sm },
  headCol: { flex: 1, alignItems: "flex-start" },
  // The name gives way (truncates, via numberOfLines) so it never wraps into
  // a second line at large text sizes.
  authorName: { fontFamily: fonts.sans, fontSize: 12, lineHeight: 16, marginBottom: 2 },
  collapsedIndent: { paddingLeft: AVATAR_GUTTER },
  deleted: { fontFamily: fonts.sans, fontSize: 14, lineHeight: 20, fontStyle: "italic" },
  reactionError: { fontFamily: fonts.sans, fontSize: 13, lineHeight: 18 },
  sessionSendRow: { flexDirection: "row", flexWrap: "wrap", gap: spacing.xs },
  image: { width: "100%", aspectRatio: 1, borderRadius: 12, maxWidth: 240, marginTop: spacing.sm },
});
