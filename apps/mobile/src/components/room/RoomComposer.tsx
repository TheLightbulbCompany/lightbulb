import Feather from "@expo/vector-icons/Feather";
import {
  activeTrigger,
  applyCompletion,
  resolveTypedMentions,
  searchEmoji,
  type RoomMember,
} from "@lightbulb/shared";
import * as ImagePicker from "expo-image-picker";
import { useCallback, useState } from "react";
import {
  ActivityIndicator,
  Animated,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
  View,
  type NativeSyntheticEvent,
  type TextInputSelectionChangeEventData,
} from "react-native";

import { colors, fonts, radii, spacing } from "../../config/theme";
import { filterOversized, type PendingAttachment } from "../../lib/attachments";
import type { ComposedMessage } from "../../lib/rooms";
import { AttachmentChips } from "../chat/AttachmentChips";
import { usePulse } from "../chat/usePulse";
import { Avatar } from "./Avatar";

/** Web composer parity: at most four images on one message. */
const MAX_PHOTOS = 4;

function ReconnectingBanner() {
  const pulse = usePulse(true, 0.3, 700);
  return (
    <View style={styles.banner}>
      <Animated.View style={[styles.bannerDot, { opacity: pulse }]} />
      <Text style={styles.bannerText}>Reconnecting live updates. You can keep sending.</Text>
    </View>
  );
}

/**
 * The composer both room screens share: multiline text, `@` member and `:`
 * emoji typeahead, a photo attachment, a send button, and the reconnecting
 * banner above it.
 *
 * The typeahead grammar is the shared one (`activeTrigger`/`applyCompletion`)
 * and so is the send-time resolver (`resolveTypedMentions`), so a token this
 * inserts notifies exactly who the web composer would.
 */
export function RoomComposer({
  members,
  placeholder,
  disabled,
  reconnecting,
  onSend,
  onUploadAttachment,
  onAbort,
}: {
  members: readonly RoomMember[];
  placeholder: string;
  disabled: boolean;
  reconnecting: boolean;
  onSend: (draft: ComposedMessage) => Promise<void>;
  onUploadAttachment: (file: {
    uri: string;
    name: string;
    type: string;
  }) => Promise<{ id: string }>;
  /**
   * Non-null while a run can be stopped. A room can hold several agents, so a
   * run in flight must never block sending — Stop only takes Send's slot while
   * the draft is empty, which is exactly when Send would be disabled anyway.
   */
  onAbort?: (() => void) | null;
}) {
  const [text, setText] = useState("");
  // Null until the field reports a collapsed caret: no caret, no typeahead.
  const [caret, setCaret] = useState<number | null>(null);
  const [focused, setFocused] = useState(false);
  const [sending, setSending] = useState(false);
  const [pending, setPending] = useState<PendingAttachment[]>([]);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const trigger = activeTrigger(text, caret);
  // The web composer's filter, verbatim: a member answers to their handle and
  // to their display name.
  const mentionOptions =
    trigger?.kind === "mention"
      ? members
          .filter((member) => {
            const needle = trigger.query.toLocaleLowerCase();
            return (
              member.displayName.toLocaleLowerCase().includes(needle) ||
              (member.mentionHandle ?? "").toLocaleLowerCase().includes(needle)
            );
          })
          .slice(0, 6)
      : [];
  const emojiOptions = trigger?.kind === "emoji" ? searchEmoji(trigger.query).slice(0, 8) : [];

  const onSelectionChange = useCallback(
    (event: NativeSyntheticEvent<TextInputSelectionChangeEventData>) => {
      const { start, end } = event.nativeEvent.selection;
      setCaret(start === end ? start : null);
    },
    [],
  );

  // ponytail: the native caret is not driven back, so completing mid-draft
  // leaves iOS's cursor at the end of the field. Fixing it needs a controlled
  // `selection` prop, which brings its own cursor-jump bugs on a multiline
  // input; the next onSelectionChange re-syncs this state either way.
  const complete = useCallback(
    (token: string) => {
      if (!trigger) return;
      const next = applyCompletion(text, trigger, token);
      setText(next.text);
      setCaret(next.caret);
    },
    [text, trigger],
  );

  const attach = useCallback(async () => {
    setError(null);
    const result = await ImagePicker.launchImageLibraryAsync({
      // Images only — the spec's non-goal is "file attachments beyond images".
      mediaTypes: ["images"],
      allowsMultipleSelection: false,
      quality: 1,
    });
    const asset = result.canceled ? undefined : result.assets?.[0];
    if (!asset) return;
    const candidate: PendingAttachment = {
      // Placeholder id: the real one is the attachment id the upload returns.
      id: asset.uri,
      name: asset.fileName ?? "photo",
      size: asset.fileSize ?? 0,
      uri: asset.uri,
      mimeType: asset.mimeType ?? "image/jpeg",
    };
    const { valid, error: oversized } = filterOversized([candidate]);
    if (oversized) setError(oversized);
    if (valid.length === 0) return;
    setUploading(true);
    try {
      const { id } = await onUploadAttachment({
        uri: candidate.uri,
        name: candidate.name,
        type: candidate.mimeType,
      });
      setPending((current) => [...current, { ...candidate, id }]);
    } catch {
      setError("Couldn't attach that photo.");
    } finally {
      setUploading(false);
    }
  }, [onUploadAttachment]);

  const removeAttachment = useCallback(
    (id: string) => setPending((current) => current.filter((item) => item.id !== id)),
    [],
  );

  const body = text.trim();
  const hasContent = !!body || pending.length > 0;
  const busy = sending || uploading;
  const submit = useCallback(async () => {
    if (!hasContent || busy || disabled) return;
    setSending(true);
    const draft: ComposedMessage = {
      text: body,
      mentions: resolveTypedMentions(body, members),
      attachmentIds: pending.map((item) => item.id),
    };
    // Cleared before the await: a failed send is drawn as its own retry row by
    // the caller, which is where the text survives.
    setText("");
    setCaret(null);
    setPending([]);
    setError(null);
    try {
      await onSend(draft);
    } finally {
      setSending(false);
    }
  }, [hasContent, busy, disabled, body, members, pending, onSend]);

  return (
    <View style={styles.outer}>
      {reconnecting && <ReconnectingBanner />}
      {(mentionOptions.length > 0 || emojiOptions.length > 0) && (
        // A strip above the card, not an overlay: the keyboard owns the bottom
        // of the screen and a popover down there would sit under it.
        <ScrollView
          horizontal
          keyboardShouldPersistTaps="always"
          showsHorizontalScrollIndicator={false}
          contentContainerStyle={styles.strip}
        >
          {mentionOptions.map((member) => (
            <Pressable
              key={`${member.kind}:${member.id}`}
              style={styles.option}
              onPress={() =>
                complete(
                  member.mentionHandle ? `@${member.mentionHandle}` : `@${member.displayName}`,
                )
              }
              accessibilityRole="button"
              accessibilityLabel={`Mention ${member.displayName}`}
            >
              <Avatar name={member.displayName} kind={member.kind} size={22} />
              <View style={styles.optionLabels}>
                <Text style={styles.optionName} numberOfLines={1}>
                  {member.displayName}
                </Text>
                <Text style={styles.optionMeta} numberOfLines={1}>
                  {member.mentionHandle ? `@${member.mentionHandle}` : "Person"}
                </Text>
              </View>
            </Pressable>
          ))}
          {emojiOptions.map((entry) => (
            <Pressable
              key={entry.emoji}
              style={styles.option}
              onPress={() => complete(entry.emoji)}
              accessibilityRole="button"
              accessibilityLabel={`Insert ${entry.emoji}`}
            >
              <Text style={styles.glyph}>{entry.emoji}</Text>
              <Text style={styles.optionName} numberOfLines={1}>
                {entry.name}
              </Text>
            </Pressable>
          ))}
        </ScrollView>
      )}
      <View style={[styles.card, focused && styles.cardFocused]}>
        {error && <Text style={styles.error}>{error}</Text>}
        <AttachmentChips attachments={pending} onRemove={removeAttachment} />
        <View style={styles.inputZone}>
          <TextInput
            style={styles.input}
            placeholder={placeholder}
            placeholderTextColor={colors.placeholder}
            value={text}
            onChangeText={setText}
            onSelectionChange={onSelectionChange}
            multiline
            editable={!disabled && !sending}
            onFocus={() => setFocused(true)}
            onBlur={() => setFocused(false)}
          />
        </View>
        <View style={styles.rail}>
          <Pressable
            style={styles.railButton}
            onPress={() => void attach()}
            disabled={disabled || busy || pending.length >= MAX_PHOTOS}
            hitSlop={6}
            accessibilityRole="button"
            accessibilityLabel="Attach photo"
          >
            {uploading ? (
              <ActivityIndicator size="small" color={colors.mute} />
            ) : (
              <Feather name="image" size={18} color={colors.mute} />
            )}
          </Pressable>
          {onAbort && !hasContent ? (
            <Pressable
              style={styles.sendButton}
              onPress={onAbort}
              accessibilityRole="button"
              accessibilityLabel="Stop"
            >
              <Feather name="square" size={14} color={colors.white} />
            </Pressable>
          ) : (
            <Pressable
              style={[styles.sendButton, (!hasContent || disabled) && styles.sendDisabled]}
              onPress={() => void submit()}
              disabled={!hasContent || disabled || busy}
              accessibilityRole="button"
              accessibilityLabel="Send"
            >
              {sending ? (
                <ActivityIndicator size="small" color={colors.white} />
              ) : (
                <Feather name="arrow-up" size={16} color={colors.white} />
              )}
            </Pressable>
          )}
        </View>
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  outer: {
    paddingHorizontal: spacing.lg,
    paddingBottom: spacing.lg,
    backgroundColor: colors.cream,
  },
  strip: {
    gap: spacing.sm,
    paddingBottom: spacing.sm,
  },
  option: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.sm,
    maxWidth: 220,
    backgroundColor: colors.white,
    borderRadius: radii.lg,
    borderWidth: 1,
    borderColor: colors.edge,
    paddingHorizontal: spacing.md,
    paddingVertical: spacing.sm,
  },
  optionLabels: { flexShrink: 1 },
  optionName: {
    fontFamily: fonts.sansMedium,
    fontSize: 13,
    lineHeight: 18,
    color: colors.ink,
    flexShrink: 1,
  },
  optionMeta: {
    fontFamily: fonts.sans,
    fontSize: 11,
    lineHeight: 15,
    color: colors.mute,
  },
  glyph: { fontSize: 18, lineHeight: 22 },
  card: {
    backgroundColor: colors.white,
    borderRadius: radii.xl,
    borderWidth: 1,
    borderColor: colors.edge,
    shadowColor: colors.ink,
    shadowOffset: { width: 0, height: 4 },
    shadowOpacity: 0.05,
    shadowRadius: 16,
    elevation: 3,
  },
  cardFocused: {
    borderColor: colors.forestFocusBorder,
    shadowOffset: { width: 0, height: 6 },
    shadowOpacity: 0.08,
    shadowRadius: 24,
    elevation: 4,
  },
  error: {
    fontFamily: fonts.sansMedium,
    fontSize: 12,
    lineHeight: 16,
    color: colors.errorText,
    paddingHorizontal: spacing.lg,
    paddingTop: spacing.md,
  },
  inputZone: { paddingHorizontal: spacing.lg, paddingTop: spacing.md },
  input: {
    fontFamily: fonts.sans,
    fontSize: 15,
    lineHeight: 24,
    maxHeight: 200,
    color: colors.ink,
    paddingTop: 0,
    paddingBottom: 0,
    textAlignVertical: "top",
  },
  rail: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.xs,
    paddingHorizontal: spacing.sm,
    paddingBottom: spacing.sm,
    paddingTop: 6,
  },
  railButton: {
    width: 32,
    height: 32,
    alignItems: "center",
    justifyContent: "center",
  },
  sendButton: {
    marginLeft: "auto",
    width: 32,
    height: 32,
    borderRadius: 16,
    backgroundColor: colors.forest,
    alignItems: "center",
    justifyContent: "center",
  },
  sendDisabled: { opacity: 0.5 },
  banner: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.sm,
    backgroundColor: colors.bannerBg,
    borderWidth: 1,
    borderColor: colors.bannerBorder,
    borderRadius: radii.md,
    paddingHorizontal: spacing.md,
    paddingVertical: 6,
    marginBottom: spacing.sm,
  },
  bannerDot: { width: 6, height: 6, borderRadius: 3, backgroundColor: colors.connectingYellow },
  bannerText: {
    flex: 1,
    fontFamily: fonts.sansMedium,
    fontSize: 12,
    lineHeight: 16,
    color: colors.bannerText,
  },
});
