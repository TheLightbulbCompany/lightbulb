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
import Reanimated, {
  interpolateColor,
  useAnimatedStyle,
  useSharedValue,
  withSpring,
} from "react-native-reanimated";
import { useSafeAreaInsets } from "react-native-safe-area-context";

import { colors, fonts, radii, spacing } from "../../config/theme";
import { filterOversized, type PendingAttachment } from "../../lib/attachments";
import type { ComposedMessage } from "../../lib/rooms";
import { AttachmentChips } from "../chat/AttachmentChips";
import { usePulse } from "../chat/usePulse";
import { Avatar } from "./Avatar";

/** Web composer parity: at most four images on one message. */
const MAX_PHOTOS = 4;

/**
 * Both springs are CRITICALLY damped (`dampingRatio: 1`) — neither is a
 * momentum gesture, so neither may overshoot. A focus ring that bounces past
 * its colour, or a button that springs back bigger than it started, reads as a
 * toy. Reanimated treats `duration` as PERCEPTUAL (actual ≈ 1.5×).
 */
const FOCUS_SPRING = { duration: 200, dampingRatio: 1 } as const;
/** Press-down has to land under the finger, so it is the faster of the two. */
const PRESS_SPRING = { duration: 120, dampingRatio: 1 } as const;
/** Pressed size for the send button — Apple's own press scale, near enough. */
const PRESS_SCALE = 0.96;

/** Five lines of the input's own 22pt leading, then it scrolls. */
const INPUT_MAX_LINES = 5;

const AnimatedPressable = Reanimated.createAnimatedComponent(Pressable);

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
  onTypingChange,
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
  /**
   * Whether the draft is non-empty, reported as it changes — web parity
   * (`RoomComposer.tsx`'s `onTypingChange`). The composer is shared by the
   * room and thread screens and cannot know which timeline it reports into,
   * so the caller supplies the reporting itself (same split as `useSendDrafts`).
   */
  onTypingChange?: (typing: boolean) => void;
}) {
  const insets = useSafeAreaInsets();
  const [text, setText] = useState("");
  // Null until the field reports a collapsed caret: no caret, no typeahead.
  const [caret, setCaret] = useState<number | null>(null);
  const [sending, setSending] = useState(false);
  const [pending, setPending] = useState<PendingAttachment[]>([]);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Focus and press live on the UI thread as shared values, not in React state:
  // the bar's hairline used to hard-swap between two StyleSheet entries on a
  // `focused` boolean, which is a visual jump, and a state flip cannot be
  // interrupted mid-way when focus bounces.
  const focusProgress = useSharedValue(0);
  const sendScale = useSharedValue(1);
  const barStyle = useAnimatedStyle(() => ({
    borderTopColor: interpolateColor(
      focusProgress.value,
      [0, 1],
      [colors.edge, colors.forestFocusBorder],
    ),
  }));
  const sendStyle = useAnimatedStyle(() => ({
    transform: [{ scale: sendScale.value }],
  }));

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
    onTypingChange?.(false);
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
  }, [hasContent, busy, disabled, body, members, pending, onSend, onTypingChange]);

  return (
    <Reanimated.View
      testID="composer-outer"
      style={[styles.outer, barStyle, { paddingBottom: Math.max(insets.bottom, spacing.sm) }]}
    >
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
      <View style={styles.card}>
        {error && <Text style={styles.error}>{error}</Text>}
        <AttachmentChips attachments={pending} onRemove={removeAttachment} />
        <View style={styles.inputZone}>
          <TextInput
            style={styles.input}
            placeholder={placeholder}
            placeholderTextColor={colors.placeholder}
            value={text}
            onChangeText={(next) => {
              setText(next);
              onTypingChange?.(!!next.trim());
            }}
            onSelectionChange={onSelectionChange}
            multiline
            editable={!disabled && !sending}
            onFocus={() => {
              focusProgress.value = withSpring(1, FOCUS_SPRING);
            }}
            onBlur={() => {
              focusProgress.value = withSpring(0, FOCUS_SPRING);
              onTypingChange?.(false);
            }}
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
            <AnimatedPressable
              style={[
                styles.sendButton,
                (!hasContent || disabled) && styles.sendDisabled,
                sendStyle,
              ]}
              // Press-DOWN feedback, not a delayed opacity on release: the
              // button has to answer the finger before the send even starts.
              onPressIn={() => {
                sendScale.value = withSpring(PRESS_SCALE, PRESS_SPRING);
              }}
              onPressOut={() => {
                sendScale.value = withSpring(1, PRESS_SPRING);
              }}
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
            </AnimatedPressable>
          )}
        </View>
      </View>
    </Reanimated.View>
  );
}

const styles = StyleSheet.create({
  // A flat bar on the page ground, delineated by one hairline against the
  // timeline above it — not a card floating over it. `borderTopColor` is left
  // to the animated style (it springs on focus); this is only its width.
  outer: {
    paddingHorizontal: spacing.sm,
    paddingTop: spacing.sm,
    backgroundColor: colors.cream,
    borderTopWidth: StyleSheet.hairlineWidth,
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
  // No radius, no border, no shadow — the shadow and the 1pt ring were what
  // made the composer read as a floating card rather than Slack's input bar.
  // The bar's own hairline (`outer`) is the only delineation now.
  card: {},
  error: {
    fontFamily: fonts.sansMedium,
    fontSize: 12,
    lineHeight: 16,
    color: colors.errorText,
    paddingHorizontal: spacing.lg,
    paddingTop: spacing.md,
  },
  inputZone: { paddingHorizontal: spacing.sm },
  input: {
    fontFamily: fonts.sans,
    fontSize: 16,
    lineHeight: 22,
    maxHeight: 22 * INPUT_MAX_LINES,
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
