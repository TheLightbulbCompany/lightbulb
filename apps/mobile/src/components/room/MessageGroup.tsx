import type { CanonicalRoomMessage, ImageAttachment } from "@lightbulb/shared";
import { useEffect, useState } from "react";
import { Image, Pressable, StyleSheet, Text, View } from "react-native";

import { colors, fonts, radii, spacing } from "../../config/theme";
import { blobToDataUri } from "../../lib/rooms";
import { MessageMarkdown } from "../chat/MessageMarkdown";
import { Avatar } from "./Avatar";

const AVATAR = 28;
/** Body indent under a collapsed run, so text lines up with the run above. */
const INDENT = AVATAR + spacing.sm;

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
 * One `message` timeline item, Slack's shape: avatar + author + time on the
 * first message of an author's run, body only on the rest. A long press
 * reveals the time of a collapsed message (and hands the press on to the
 * caller, which is where the actions sheet lands).
 */
export function MessageGroup({
  message,
  collapsed,
  replyCount,
  onLongPress,
  onOpenThread,
  downloadAttachment,
}: {
  message: CanonicalRoomMessage;
  collapsed: boolean;
  replyCount: number;
  onLongPress?: () => void;
  onOpenThread?: () => void;
  downloadAttachment: (roomId: string, attachmentId: string) => Promise<Blob>;
}) {
  const [timeShown, setTimeShown] = useState(false);
  const time = formatTime(message.createdAt);
  return (
    <Pressable
      style={collapsed ? styles.collapsed : styles.group}
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
          <MessageMarkdown text={message.text} />
        )}
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

const styles = StyleSheet.create({
  group: { marginTop: spacing.lg },
  collapsed: { marginTop: spacing.xs, paddingLeft: INDENT },
  head: { flexDirection: "row", alignItems: "center", gap: spacing.sm },
  author: { fontFamily: fonts.sansSemiBold, fontSize: 13, lineHeight: 18, color: colors.ink },
  time: { fontFamily: fonts.sans, fontSize: 11, lineHeight: 16, color: colors.mute },
  collapsedTime: { marginBottom: 2 },
  body: { paddingLeft: INDENT, marginTop: 2 },
  deleted: { fontFamily: fonts.sans, fontSize: 14, lineHeight: 20, color: colors.mute, fontStyle: "italic" },
  replies: { fontFamily: fonts.sansMedium, fontSize: 13, lineHeight: 18, color: colors.forest, marginTop: spacing.xs },
  image: { width: 220, height: 220, borderRadius: radii.md, marginTop: spacing.sm },
  imagePending: { backgroundColor: colors.skeleton },
});
