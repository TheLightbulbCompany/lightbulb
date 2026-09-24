import { SymbolView, type SFSymbol } from "expo-symbols";
import {
  activeTrigger,
  applyCompletion,
  MESSAGE_ATTACHMENT_MAX_BYTES,
  resolveMessageAttachmentContentType,
  resolveTypedMentions,
  respondHint,
  searchEmoji,
  wrapSelection,
  type MarkdownStyle,
  type RoomKind,
  type RoomMember,
} from "@lightbulb/shared";
import * as DocumentPicker from "expo-document-picker";
import * as ImagePicker from "expo-image-picker";
import { useCallback, useRef, useState } from "react";
import {
  ActivityIndicator,
  ActionSheetIOS,
  Alert,
  Animated,
  Platform,
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
import { checkPhoto, useComposerDraft } from "../../lib/composerDraft";
import type { ComposedMessage } from "../../lib/rooms";
import { useScaledHeight } from "../../lib/textScale";
import { hapticSelection } from "../../lib/haptics";
import { AttachmentChips } from "../chat/AttachmentChips";
import { usePulse } from "../chat/usePulse";
import { Avatar } from "./Avatar";
import { EmojiPickerSheet } from "./EmojiPickerSheet";

/** Web composer parity: at most four attachments on one message. */
const MAX_ATTACHMENTS = 4;
const XLSX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet";

/**
 * The formatting toolbar, in the web composer's order. Icon-only: a phone has
 * no room for labels above the keyboard, and these are the glyphs iOS readers
 * already know from Notes and Mail.
 */
const FORMAT_CONTROLS: { style: MarkdownStyle; icon: SFSymbol; label: string }[] = [
  { style: "bold", icon: "bold", label: "Bold" },
  { style: "italic", icon: "italic", label: "Italic" },
  { style: "strike", icon: "strikethrough", label: "Strikethrough" },
  { style: "code", icon: "chevron.left.forwardslash.chevron.right", label: "Code" },
  { style: "codeblock", icon: "curlybraces", label: "Code block" },
  { style: "ul", icon: "list.bullet", label: "Bulleted list" },
  { style: "ol", icon: "list.number", label: "Numbered list" },
  { style: "link", icon: "link", label: "Link" },
];

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

/** Five lines of the input's own 22pt leading (scaled by the reader's text
 * size), then it scrolls. */
const INPUT_MAX_LINES = 5;
/** Two lines of the input's leading at rest. */
const INPUT_MIN_LINES = 2;

const AnimatedPressable = Reanimated.createAnimatedComponent(Pressable);

/** Keeps a remembered range inside text that may have changed since. */
function clampRange(range: { start: number; end: number }, length: number) {
  const start = Math.max(0, Math.min(range.start, length));
  return { start, end: Math.max(start, Math.min(range.end, length)) };
}

type PickedPhoto = ImagePicker.ImagePickerAsset;

// ponytail: HEIC/HEIF are detected but the messaging endpoint does not accept
// them, so `checkPhoto` turns one into a clear "format we can't send" rather
// than a server rejection reported as a network error. The picker re-encodes
// to JPEG on the paths we use, so this should stay unreachable; if it ever
// fires for real, transcode rather than widening the endpoint.
function photoMimeType(asset: PickedPhoto): string | null {
  if (asset.mimeType) return asset.mimeType.startsWith("image/") ? asset.mimeType : null;
  const name = asset.fileName ?? asset.uri;
  if (/\.jpe?g$/i.test(name)) return "image/jpeg";
  if (/\.png$/i.test(name)) return "image/png";
  if (/\.gif$/i.test(name)) return "image/gif";
  if (/\.webp$/i.test(name)) return "image/webp";
  if (/\.heic$/i.test(name)) return "image/heic";
  if (/\.heif$/i.test(name)) return "image/heif";
  return null;
}

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
  draftKey,
  roomKind,
  runningAgentIds,
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
  /**
   * Identifies the conversation this draft belongs to (`lib/composerDraft`'s
   * `draftKey`). A room's own timeline and each of its threads draft
   * separately. `null` while the room is not yet known, which just means
   * nothing is kept.
   */
  draftKey: string | null;
  /** For the "who will answer" line; omitted on surfaces that have no room. */
  roomKind?: RoomKind;
  /** Agents mid-run right now — they answer whether or not they are addressed. */
  runningAgentIds?: readonly string[];
}) {
  const insets = useSafeAreaInsets();
  const inputMaxHeight = useScaledHeight(22 * INPUT_MAX_LINES);
  // Room for two lines at rest, so the field reads as somewhere to write rather
  // than a single slot pinned under the hairline.
  const inputMinHeight = useScaledHeight(22 * INPUT_MIN_LINES);
  const draft = useComposerDraft({ key: draftKey, uploadAttachment: onUploadAttachment });
  const { text, setText } = draft;
  // The text as of RIGHT NOW, not as of the last render: `applyEdit` writes it
  // ahead of React so a second edit in the same tick reads what the first one
  // produced. Re-synced here for changes that come from elsewhere — a restored
  // draft, a send clearing the field.
  const textRef = useRef(text);
  textRef.current = text;
  // Null until the field reports a collapsed caret: no caret, no typeahead.
  const [caret, setCaret] = useState<number | null>(null);
  // The live selection, and a selection we are asking the field to adopt.
  const selection = useRef({ start: 0, end: 0 });
  // Whether the reader has ever put a cursor in the field.
  const touched = useRef(false);
  const [pendingSelection, setPendingSelection] = useState<{ start: number; end: number } | null>(
    null,
  );
  const [emojiOpen, setEmojiOpen] = useState(false);
  // Formatting is hidden until the reader asks for it (Slack's Aa).
  const [formatOpen, setFormatOpen] = useState(false);
  const [sending, setSending] = useState(false);
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

  // The toolbar wraps whatever is selected, so the whole range is tracked, not
  // only the collapsed caret the typeahead needs.
  const onSelectionChange = useCallback(
    (event: NativeSyntheticEvent<TextInputSelectionChangeEventData>) => {
      const { start, end } = event.nativeEvent.selection;
      selection.current = { start, end };
      touched.current = true;
      setCaret(start === end ? start : null);
      // Released on the FIRST report, whatever it says — either iOS has taken
      // the caret we asked for, or the reader has moved it themselves, and in
      // both cases continuing to drive it fights them. Waiting for an exact
      // match instead meant a reader who tapped elsewhere before the echo
      // arrived was never released at all, and every later render snapped the
      // caret back to wherever the toolbar had left it.
      setPendingSelection(null);
    },
    [],
  );

  /**
   * Applies an edit that also moves the caret — the toolbar, a typeahead
   * completion, an emoji insert.
   *
   * The new text and selection are written to their refs HERE, synchronously,
   * rather than waiting for iOS to echo them back. That echo is a round trip,
   * and two taps fit inside one easily: a second edit reading the pre-edit
   * offsets would slice a string that had already changed under it, and since
   * this setter is the persisted draft's, the corruption would be written to
   * disk and restored the next time the room was opened.
   */
  const applyEdit = useCallback(
    (next: { text: string; start: number; end: number }) => {
      textRef.current = next.text;
      selection.current = { start: next.start, end: next.end };
      setCaret(next.start === next.end ? next.start : null);
      setText(next.text);
      setPendingSelection({ start: next.start, end: next.end });
    },
    [setText],
  );

  /**
   * Wraps the selection in markdown, the way the web toolbar does — the same
   * shared `wrapSelection`, so `**bold**` here and there mean the same thing.
   *
   * The resulting caret is driven back into the field, which is what makes an
   * empty selection useful: tapping Bold inserts `****` and leaves the cursor
   * between the markers, ready to type.
   */
  const format = useCallback(
    (style: MarkdownStyle) => {
      const current = textRef.current;
      // A field nobody has put a cursor in yet gets one at the end, not at the
      // start — wrapping the front of a draft you just came back to is never
      // what was meant.
      const { start, end } = touched.current
        ? clampRange(selection.current, current.length)
        : { start: current.length, end: current.length };
      const next = wrapSelection(current, start, end, style);
      applyEdit({ text: next.text, start: next.selectionStart, end: next.selectionEnd });
      hapticSelection();
    },
    [applyEdit],
  );

  const complete = useCallback(
    (token: string) => {
      if (!trigger) return;
      const next = applyCompletion(textRef.current, trigger, token);
      applyEdit({ text: next.text, start: next.caret, end: next.caret });
    },
    [trigger, applyEdit],
  );

  const stagePhoto = useCallback(
    (asset: PickedPhoto) => {
      setError(null);
      const mimeType = photoMimeType(asset);
      if (!mimeType) {
        setError("That file isn't a photo we can send. Choose a JPEG, PNG, GIF or WebP image.");
        return;
      }
      // The messaging endpoint's own cap and formats, not the container-file
      // rail's: sending a 6MB photo used to pass here and fail on the server,
      // which reported it as a connection problem.
      const { contentType, error: rejected } = checkPhoto(mimeType, asset.fileSize ?? 0);
      if (rejected != null) {
        setError(rejected);
        return;
      }
      draft.stage({
        uri: asset.uri,
        name: asset.fileName ?? "photo",
        contentType,
        size: asset.fileSize ?? 0,
      });
    },
    [draft],
  );

  const pickFromLibrary = useCallback(async () => {
    try {
      const permission = await ImagePicker.requestMediaLibraryPermissionsAsync();
      if (!permission.granted) {
        setError("Photo library access is needed. Allow it in Settings, then try again.");
        return;
      }
      const result = await ImagePicker.launchImageLibraryAsync({
        mediaTypes: ["images"],
        allowsMultipleSelection: false,
        quality: 1,
      });
      const asset = result.canceled ? undefined : result.assets?.[0];
      if (asset) stagePhoto(asset);
    } catch {
      setError("Couldn't open your photo library. Try again.");
    }
  }, [stagePhoto]);

  const takePhoto = useCallback(async () => {
    try {
      const permission = await ImagePicker.requestCameraPermissionsAsync();
      if (!permission.granted) {
        setError("Camera access is needed. Allow it in Settings, then try again.");
        return;
      }
      const result = await ImagePicker.launchCameraAsync({
        mediaTypes: ["images"],
        quality: 1,
      });
      const asset = result.canceled ? undefined : result.assets?.[0];
      if (asset) stagePhoto(asset);
    } catch {
      setError("Couldn't open the camera. Try again.");
    }
  }, [stagePhoto]);

  const pickFile = useCallback(async () => {
    try {
      const result = await DocumentPicker.getDocumentAsync({
        type: [XLSX_CONTENT_TYPE],
        multiple: false,
        copyToCacheDirectory: true,
      });
      const asset = result.canceled ? undefined : result.assets?.[0];
      if (!asset) return;
      const contentType = resolveMessageAttachmentContentType(asset.name, asset.mimeType ?? "");
      if (contentType !== XLSX_CONTENT_TYPE) {
        setError("That file isn't an XLSX workbook we can send.");
        return;
      }
      const size = asset.size ?? 0;
      if (size > MESSAGE_ATTACHMENT_MAX_BYTES[contentType]) {
        setError("XLSX files must be 20 MiB or smaller.");
        return;
      }
      setError(null);
      draft.stage({ uri: asset.uri, name: asset.name, contentType, size });
    } catch {
      setError("Couldn't open your files. Try again.");
    }
  }, [draft]);

  const attach = useCallback(() => {
    setError(null);
    if (Platform.OS === "ios") {
      ActionSheetIOS.showActionSheetWithOptions(
        { options: ["Photo Library", "Take Photo", "Choose File", "Cancel"], cancelButtonIndex: 3 },
        (index) => {
          if (index === 0) void pickFromLibrary();
          if (index === 1) void takePhoto();
          if (index === 2) void pickFile();
        },
      );
      return;
    }
    Alert.alert("Attach file", undefined, [
      { text: "Photo Library", onPress: () => void pickFromLibrary() },
      { text: "Take Photo", onPress: () => void takePhoto() },
      { text: "Choose File", onPress: () => void pickFile() },
      { text: "Cancel", style: "cancel" },
    ]);
  }, [pickFile, pickFromLibrary, takePhoto]);

  const body = text.trim();
  const mentions = resolveTypedMentions(body, members);
  // The web composer's three limits, verbatim — the server enforces all of
  // them, and being told before you send beats a rejection after.
  const limit =
    new TextEncoder().encode(text).length > 100_000
      ? "Keep your message within 100,000 UTF-8 bytes."
      : mentions.length > 32
        ? "Mention at most 32 room members."
        : null;
  // Who this message will actually reach, by the same rule the backend
  // dispatches on — so the answer is not a guess the two could disagree about.
  const hint = respondHint(roomKind, members, mentions, runningAgentIds ?? []);
  const hasContent = !!body || draft.attachments.length > 0;
  // Photos upload as they are picked, so sending waits only on the ones still
  // in flight — and a failed one has to be retried or removed first, rather
  // than silently dropping out of the message.
  const busy = sending || draft.uploading;
  const submit = useCallback(async () => {
    if (!hasContent || busy || disabled || limit || draft.failed) return;
    setSending(true);
    onTypingChange?.(false);
    setError(null);
    const message: ComposedMessage = {
      text: body,
      mentions,
      attachmentIds: draft.attachmentIds,
    };
    draft.clear();
    setCaret(null);
    // A sent message ends the formatting session; the next one starts clean.
    setFormatOpen(false);
    try {
      await onSend(message);
    } finally {
      setSending(false);
    }
  }, [hasContent, busy, disabled, limit, body, mentions, draft, onSend, onTypingChange]);

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
        {(error ?? limit) != null && <Text style={styles.error}>{error ?? limit}</Text>}
        <AttachmentChips
          attachments={draft.attachments}
          onRemove={draft.remove}
          onRetry={draft.retry}
        />
        <View style={styles.inputZone}>
          <TextInput
            style={[styles.input, { minHeight: inputMinHeight, maxHeight: inputMaxHeight }]}
            placeholder={placeholder}
            placeholderTextColor={colors.placeholder}
            value={text}
            onChangeText={(next) => {
              setText(next);
              onTypingChange?.(!!next.trim());
            }}
            onSelectionChange={onSelectionChange}
            selection={pendingSelection ?? undefined}
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
        {/* Formatting stays out of the way until asked for, the way Slack's Aa
            does. Always on, it took a full row from the one thing the composer
            is for: on a phone with the keyboard up, the message got a single
            line while three rows of controls sat under it. The row scrolls
            rather than wrapping when the text size grows. */}
        {formatOpen && (
          <ScrollView
            horizontal
            keyboardShouldPersistTaps="always"
            showsHorizontalScrollIndicator={false}
            contentContainerStyle={styles.formatRow}
          >
            {FORMAT_CONTROLS.map((control) => (
              <Pressable
                key={control.style}
                style={styles.formatButton}
                onPress={() => format(control.style)}
                disabled={disabled || sending}
                hitSlop={4}
                accessibilityRole="button"
                accessibilityLabel={control.label}
              >
                <SymbolView
                  accessibilityElementsHidden
                  name={control.icon}
                  size={16}
                  tintColor={colors.mute}
                />
              </Pressable>
            ))}
          </ScrollView>
        )}
        <View style={styles.rail}>
          <Pressable
            style={[styles.railButton, formatOpen && styles.railButtonActive]}
            onPress={() => {
              hapticSelection();
              setFormatOpen((open) => !open);
            }}
            disabled={disabled || sending}
            hitSlop={6}
            accessibilityRole="button"
            accessibilityLabel="Formatting"
            accessibilityState={{ expanded: formatOpen }}
          >
            <SymbolView
              accessibilityElementsHidden
              name="textformat"
              size={18}
              tintColor={formatOpen ? colors.ink : colors.mute}
            />
          </Pressable>
          <Pressable
            style={styles.railButton}
            onPress={() => setEmojiOpen(true)}
            disabled={disabled || sending}
            hitSlop={6}
            accessibilityRole="button"
            accessibilityLabel="Insert emoji"
          >
            <SymbolView accessibilityElementsHidden name="face.smiling" size={18} tintColor={colors.mute} />
          </Pressable>
          <Pressable
            style={styles.railButton}
            onPress={attach}
            disabled={disabled || busy || draft.attachments.length >= MAX_ATTACHMENTS}
            hitSlop={6}
            accessibilityRole="button"
            accessibilityLabel="Attach files"
          >
            {draft.uploading ? (
              <ActivityIndicator size="small" color={colors.mute} />
            ) : (
              <SymbolView accessibilityElementsHidden name="paperclip" size={18} tintColor={colors.mute} />
            )}
          </Pressable>
          {/* Who will answer, in the rail's empty middle rather than a row of
              its own — the last thing read before sending, without costing the
              message any height. */}
          <Text style={styles.hint} maxFontSizeMultiplier={1.4} numberOfLines={1}>
            {hint}
          </Text>
          {onAbort && !hasContent ? (
            <Pressable
              style={styles.sendButton}
              onPress={onAbort}
              accessibilityRole="button"
              accessibilityLabel="Stop"
            >
              <SymbolView accessibilityElementsHidden name="stop.fill" size={14} tintColor={colors.white} />
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
                <SymbolView accessibilityElementsHidden name="arrow.up" size={16} tintColor={colors.white} />
              )}
            </AnimatedPressable>
          )}
        </View>
      </View>
      <EmojiPickerSheet
        visible={emojiOpen}
        onClose={() => setEmojiOpen(false)}
        onPick={(emoji) => {
          const current = textRef.current;
          const { start, end } = touched.current
            ? clampRange(selection.current, current.length)
            : { start: current.length, end: current.length };
          const caretAfter = start + emoji.length;
          applyEdit({
            text: `${current.slice(0, start)}${emoji}${current.slice(end)}`,
            start: caretAfter,
            end: caretAfter,
          });
          setEmojiOpen(false);
        }}
      />
    </Reanimated.View>
  );
}

const styles = StyleSheet.create({
  formatRow: { flexDirection: "row", gap: spacing.xs, paddingHorizontal: spacing.sm },
  formatButton: {
    width: 32,
    height: 32,
    alignItems: "center",
    justifyContent: "center",
    borderRadius: radii.md,
  },
  // Takes the rail's empty middle and gives way first when it gets tight.
  hint: {
    flex: 1,
    fontFamily: fonts.sans,
    fontSize: 12,
    lineHeight: 16,
    color: colors.mute,
    textAlign: "right",
    paddingHorizontal: spacing.xs,
  },
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
  // Breathing room above and below the text, so a message is not written
  // flush against the bar's hairline.
  inputZone: { paddingHorizontal: spacing.sm, paddingVertical: spacing.sm },
  input: {
    fontFamily: fonts.sans,
    fontSize: 16,
    lineHeight: 22,
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
    borderRadius: radii.md,
  },
  railButtonActive: { backgroundColor: colors.chip },
  sendButton: {
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
