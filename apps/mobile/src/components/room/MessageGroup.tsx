import {
  isVideoAttachmentType,
  displaySessionMentions,
  linkifyMentions,
  linkifySessionMentions,
  sessionLabel,
  sessionStatus,
  unpromptedLabel,
  type AvatarRing,
  type CardReply,
  type CanonicalRoomMessage,
  type MessageAttachment,
  type RoomMember,
  type LocalSession,
  type SessionMentionCandidate,
} from "@lightbulb/shared";
import { Image } from "expo-image";
import { router } from "expo-router";
import { useEffect, useMemo, useState } from "react";
import { Pressable, StyleSheet, Text, View } from "react-native";

import { fonts, spacing, useChatPalette, type ChatPalette } from "../../config/theme";
import { hapticImpact, hapticSelection } from "../../lib/haptics";
import { attachmentFile } from "../../lib/attachmentFile";
import type { LocalMessageRow } from "../../lib/rooms";
import { Bubble, BubbleMeta, Chip, MetaText, AVATAR_GUTTER } from "../conversation/bubbles";
import { specialistLabel } from "@lightbulb/shared";
import { useLiveEvents, useSuperAgent } from "../../providers/live-events";
import { AutomationConfirmCard } from "../tasks/AutomationConfirmCard";
import { MessageTasks, RoutedToLine } from "../tasks/TaskCard";
import { MessageMarkdown } from "../chat/MessageMarkdown";
import { CardReplyContext, useCardPressState, type CardReplyValue } from "../chat/openui/CardReplyContext";
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
  isAgentId,
}: {
  reactions: [string, string[]][];
  currentUserId: string;
  palette: ChatPalette;
  onToggle: (emoji: string, active: boolean) => void;
  /** Super-agent: a reaction only agents applied (Lightbulb's 👀) is a receipt, shown but not a toggle. */
  isAgentId?: (id: string) => boolean;
}) {
  return (
    <>
      {reactions.map(([emoji, users]) => {
        if (isAgentId && users.length > 0 && users.every(isAgentId)) {
          return (
            <View key={emoji} accessible accessibilityRole="image" accessibilityLabel={emoji === "👀" ? "Lightbulb saw this" : `Lightbulb reacted ${emoji}`}>
              <Chip label={emoji} palette={palette} />
            </View>
          );
        }
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
/** A session chip is a reference, like a document link: it never messaged the session. */
function SessionCard({ sessionId, label, session }: { sessionId: string; label?: string; session?: LocalSession }) {
  const palette = useChatPalette();
  const name = session ? sessionLabel(session) : label?.split(" · ").slice(0, -1).join(" · ") || "Session";
  const status = session ? sessionStatus[session.status] : null;
  // Open always routes by the stored id: a session that left the workspace list (ended more
  // than a week ago) still opens by id, and the session screen says so when access is really gone.
  return <Pressable accessibilityRole="button" accessibilityLabel={status ? `Open session ${name}, ${status}` : `Open session ${name}`}
    onPress={() => router.push({ pathname: "/(app)/session/[sessionId]", params: { sessionId } })}
    style={[styles.sessionCard, { borderColor: palette.edge }]}>
    <Text style={[styles.sessionActionText, { color: palette.fg }]}>{name}</Text>
    {status ? <MetaText palette={palette}>{status}</MetaText> : null}
    <Text style={[styles.sessionActionText, { color: palette.link }]}>Open session</Text>
  </Pressable>;
}

export function MessageGroup({
  message,
  collapsed,
  replyCount,
  currentUserId,
  onLongPress,
  onOpenThread,
  onToggleReaction,
  reactionError = false,
  attachmentUrl,
  members = [],
  sessionCandidates = [],
  showTime = true,
  readLabel = null,
  agentRing = null,
  onCardReply,
  cardAnswer = null,
  cardsEnabled = true,
}: {
  message: CanonicalRoomMessage;
  collapsed: boolean;
  replyCount: number;
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
  /** Default true; pass false when the next bubble continues this sender's run, so the time waits for the run's last bubble (or a long press). */
  showTime?: boolean;
  /** "Sent" | "Delivered" | "Read" | "Read by N" — only ever shown on the viewer's own last message, and never on a deleted one. */
  readLabel?: string | null;
  /** Ring on this bubble's avatar (the agent is running right now). Ignored for a mine bubble, which never draws an avatar. */
  agentRing?: AvatarRing | null;
  /** Posts a card's answer as the viewer. Absent (archived room, no access) leaves cards read-only. */
  onCardReply?: (reply: CardReply) => Promise<void>;
  /** The first human reply to this message when it carries a card (`cardAnswersFor`). */
  cardAnswer?: string | null;
  /** False in the feed: a card fence stays code. */
  cardsEnabled?: boolean;
}) {
  const palette = useChatPalette();
  const superAgent = useSuperAgent();
  const [timeShown, setTimeShown] = useState(false);
  const [viewerIndex, setViewerIndex] = useState<number | null>(null);
  const mine = message.author.kind === "human" && message.author.id === currentUserId;
  const deleted = Boolean(message.deletedAt);
  // A local (not-yet-canonical) row -- `useSendDrafts` assigns it seq 0 until
  // the server echoes the real one (the same "not live yet" convention
  // `lastOwnMessageId` reads). Dimmed regardless of whether it's still
  // in flight or has failed; `LocalMessageRows` renders the difference below it.
  const pending = message.messageSeq <= 0;
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
  const markdownText = message.via === "lightbulb_mcp" ? message.text : linkifySessionMentions(
    linkifyMentions(message.text, message.mentions, members), message,
    sessionCandidates.map(({ session }) => session),
  );
  // Cards are live only for an AGENT's message, and only while its author is still in the room.
  const authorInRoom = members.some((m) => m.kind === "agent" && m.id === message.author.id);
  const cardReply = useMemo(
    () => onCardReply && authorInRoom
      ? (text: string, clientMessageId: string) => onCardReply({ text, agentId: message.author.id, messageId: message.messageId, threadRootMessageId: message.threadRootMessageId, clientMessageId })
      : undefined,
    [onCardReply, authorInRoom, message.author.id, message.messageId, message.threadRootMessageId],
  );
  const cardPress = useCardPressState(message.messageId, currentUserId, cardReply, cardAnswer);
  const cardContext = useMemo<CardReplyValue | null>(
    () => !cardsEnabled || message.author.kind !== "agent" || deleted ? null : {
      messageId: message.messageId, roomId: message.roomId, authorId: message.author.id,
      threadRootMessageId: message.threadRootMessageId, attachments: message.attachments ?? [], ...cardPress, attachmentUrl,
    },
    [cardsEnabled, deleted, message.author.kind, message.author.id, message.messageId, message.roomId, message.threadRootMessageId, message.attachments, cardPress, attachmentUrl],
  );
  const bubbleLabel = deleted
    ? "Message deleted"
    : `${mine ? "You" : message.author.displayName}: ${message.via === "lightbulb_mcp" ? message.text : displaySessionMentions(message.text, message, sessionCandidates.map(({ session }) => session))}`;

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
                {superAgent && message.specialist ? <SpecialistName specialist={message.specialist} />
                  : superAgent && message.author.kind === "agent" ? <AgentName id={message.author.id} name={message.author.displayName} />
                  : message.author.displayName}
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
                  <CardReplyContext.Provider value={cardContext}>
                    <MessageMarkdown text={markdownText} palette={palette} />
                  </CardReplyContext.Provider>
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
              {!deleted && superAgent && message.taskRefs?.length ? (
                <MessageTasks message={message} viewerId={currentUserId} palette={palette} />
              ) : null}
              {!deleted && superAgent && message.kind === "automation_confirm" && message.automationId ? (
                <AutomationConfirmCard automationId={message.automationId} palette={palette} />
              ) : null}
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
                <CardReplyContext.Provider value={cardContext}>
                  <MessageMarkdown text={markdownText} palette={palette} inverse={mine} />
                </CardReplyContext.Provider>
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
          {!deleted && superAgent && message.routedTo ? <RoutedToLine message={message} palette={palette} /> : null}
          {!deleted && onToggleReaction && appliedReactions.length > 0 ? (
            <ReactionChips
              reactions={appliedReactions}
              currentUserId={currentUserId}
              palette={palette}
              onToggle={onToggleReaction}
              isAgentId={superAgent ? (id) => members.some((m) => m.kind === "agent" && m.id === id) : undefined}
            />
          ) : null}
          {!deleted && reactionError ? (
            <Text accessibilityRole="alert" style={[styles.reactionError, { color: palette.alert }]}>
              Could not update reaction. Try again.
            </Text>
          ) : null}
          {!deleted && !!message.sessionMentions?.length ? <View style={styles.sessionSendRow}>
            {message.sessionMentions.map((sessionId) => <SessionCard key={sessionId} sessionId={sessionId} label={message.sessionTargetLabels?.[sessionId]}
              session={sessionCandidates.find(({ session }) => session.sessionId === sessionId)?.session} />)}
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
  sessionCard: { flexDirection: "row", flexWrap: "wrap", alignItems: "center", gap: spacing.sm, borderWidth: StyleSheet.hairlineWidth, borderRadius: 8, paddingHorizontal: spacing.sm, paddingVertical: spacing.xs },
  sessionActionText: { fontFamily: fonts.sansSemiBold, fontSize: 12, lineHeight: 18 },
  metaText: { fontFamily: fonts.sans, fontSize: 12, lineHeight: 18 },
  image: { width: "100%", aspectRatio: 1, borderRadius: 12, maxWidth: 240, marginTop: spacing.sm },
});

/** "Lightbulb · Finance": a specialist id reads by its name from the agents list (an id is never shown). */
function SpecialistName({ specialist }: { specialist: string }) {
  const { agentNames } = useLiveEvents();
  return <>{specialistLabel(specialist, agentNames)}</>;
}

/** Super-agent: the front agent reads "Lightbulb" on its older posts too (stored as its seat name, "CEO"). */
function AgentName({ id, name }: { id: string; name: string }) {
  const { frontAgentId } = useLiveEvents();
  return <>{id === frontAgentId ? "Lightbulb" : name}</>;
}
