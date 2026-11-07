import { SymbolView, type SFSymbol } from "../ui/SymbolView";
import {
  activeTrigger,
  applyCompletion,
  resolveTypedMentions,
  resolveTypedSessionMentions,
  respondHint,
  searchEmoji,
  wrapSelection,
  type MarkdownStyle,
  type RoomAttachmentInput,
  type RoomKind,
  type RoomMember,
  type SessionMentionCandidate,
} from "@lightbulb/shared";
import * as Clipboard from "expo-clipboard";
import { TextInputWrapper, type PasteEventPayload } from "expo-paste-input";
import { File, Paths } from "expo-file-system";
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

import { fonts, radii, spacing, useChatPalette, type ChatPalette } from "../../config/theme";
import { asJpeg } from "../../lib/attachmentFile";
import { checkPhoto, useComposerDraft } from "../../lib/composerDraft";
import type { ComposedMessage } from "../../lib/rooms";
import { useScaledHeight } from "../../lib/textScale";
import { hapticSelection } from "../../lib/haptics";
import { AttachmentChips } from "../chat/AttachmentChips";
import { usePulse } from "../chat/usePulse";
import { EmojiPickerSheet } from "./EmojiPickerSheet";

// The photo as it is on the phone. What the endpoint will not take is
// re-encoded once, by `stagePhoto`, not compressed here and again there.
const PHOTO_OPTIONS = {
  mediaTypes: ["images"],
  quality: 1,
} satisfies ImagePicker.ImagePickerOptions;

/** Web composer parity: at most four images on one message. */
const MAX_PHOTOS = 4;

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
/** One line at rest, the way Messages draws its field; it grows as you write. */
const INPUT_MIN_LINES = 1;

const AnimatedPressable = Reanimated.createAnimatedComponent(Pressable);

/** Keeps a remembered range inside text that may have changed since. */
function clampRange(range: { start: number; end: number }, length: number) {
  const start = Math.max(0, Math.min(range.start, length));
  return { start, end: Math.max(start, Math.min(range.end, length)) };
}

/** What staging needs of a photo; a pasted one has no picker asset behind it. */
type PickedPhoto = Pick<
  ImagePicker.ImagePickerAsset,
  "uri" | "fileName" | "mimeType" | "fileSize" | "width" | "height"
>;

// An iPhone's own photos are HEIC, which the messaging endpoint does not take;
// `stagePhoto` re-encodes those as JPEG.
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

function ReconnectingBanner({ palette }: { palette: ChatPalette }) {
  const pulse = usePulse(true, 0.3, 700);
  return (
    <View style={[styles.banner, { backgroundColor: palette.raised, borderColor: palette.warn }]}>
      <Animated.View style={[styles.bannerDot, { backgroundColor: palette.warn, opacity: pulse }]} />
      <Text style={[styles.bannerText, { color: palette.warn }]}>
        Reconnecting live updates. You can keep sending.
      </Text>
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
export interface RoomComposerSessionOption {
  candidate: SessionMentionCandidate;
  ownerName: string;
}

export function RoomComposer({
  members,
  sessionTargets = [],
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
  sessionTargets?: readonly RoomComposerSessionOption[];
  placeholder: string;
  disabled: boolean;
  reconnecting: boolean;
  onSend: (draft: ComposedMessage) => Promise<void>;
  onUploadAttachment: (file: RoomAttachmentInput) => Promise<{ id: string }>;
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
  const palette = useChatPalette();
  const inputMaxHeight = useScaledHeight(22 * INPUT_MAX_LINES);
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
  const [staleNotice, setStaleNotice] = useState("");
  // The readable token is presentation; this binding keeps the selected native session as the routing key.
  const [selectedSessions, setSelectedSessions] = useState<Record<string, string>>({});

  // Focus and press live on the UI thread as shared values, not in React state:
  // a `focused` boolean hard-swaps two StyleSheet entries, which is a visual
  // jump, and a state flip cannot be interrupted mid-way when focus bounces.
  // Focus is the FIELD's outline firming up, not a line across the screen.
  const focusProgress = useSharedValue(0);
  const sendScale = useSharedValue(1);
  const pillStyle = useAnimatedStyle(() => ({
    borderColor: interpolateColor(focusProgress.value, [0, 1], [palette.edge, palette.fgMute]),
  }));
  const sendStyle = useAnimatedStyle(() => ({
    transform: [{ scale: sendScale.value }],
  }));

  const trigger = activeTrigger(text, caret);
  // The @ picker is gone (Phase 1): a typed `@handle` still resolves at send.
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

  // A photo being re-encoded is not in the draft yet. Send waits for it, or
  // the message would leave without the photo and the photo would land in
  // the next, empty draft.
  const [preparing, setPreparing] = useState(false);
  // Paste events can arrive again before React commits the attachments from
  // the first batch. Guard synchronously so both callbacks cannot reserve the
  // same four slots from one stale render.
  const directPasteInFlight = useRef(false);
  const stagePhoto = useCallback(
    async (asset: PickedPhoto) => {
      setError(null);
      const mimeType = photoMimeType(asset);
      if (!mimeType) {
        setError("That file isn't a photo we can send. Choose a JPEG, PNG, GIF or WebP image.");
        return;
      }
      let photo = {
        uri: asset.uri,
        name: asset.fileName ?? "photo",
        mimeType,
        size: asset.fileSize ?? 0,
      };
      // A photo the endpoint takes goes as it is, so a screenshot stays a
      // sharp PNG. One it does not take is scaled down and sent as a JPEG. A
      // GIF or WebP is left alone: either can be animated, and re-encoding
      // would keep only the first frame.
      const animated = mimeType === "image/gif" || mimeType === "image/webp";
      if (checkPhoto(photo.mimeType, photo.size).error != null && !animated) {
        setPreparing(true);
        try {
          photo = { ...(await asJpeg({ ...asset, name: photo.name })), mimeType: "image/jpeg" };
        } catch {
          setError("Couldn't prepare that photo. Try another one.");
          return;
        } finally {
          setPreparing(false);
        }
      }
      const { contentType, error: rejected } = checkPhoto(photo.mimeType, photo.size);
      if (rejected != null) {
        setError(rejected);
        return;
      }
      draft.stage({ uri: photo.uri, name: photo.name, contentType, size: photo.size });
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
        ...PHOTO_OPTIONS,
        allowsMultipleSelection: false,
      });
      const asset = result.canceled ? undefined : result.assets?.[0];
      if (asset) await stagePhoto(asset);
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
      const result = await ImagePicker.launchCameraAsync(PHOTO_OPTIONS);
      const asset = result.canceled ? undefined : result.assets?.[0];
      if (asset) await stagePhoto(asset);
    } catch {
      setError("Couldn't open the camera. Try again.");
    }
  }, [stagePhoto]);

  // The text field's own Paste takes text only, so a copied photo comes in
  // from here. As a JPEG: a photo read as a PNG is tens of megabytes.
  const pastePhoto = useCallback(async () => {
    // Reading a large photo off the clipboard takes a moment too.
    setPreparing(true);
    try {
      const image = await Clipboard.getImageAsync({ format: "jpeg", jpegQuality: 0.8 });
      // Offered only with a photo on the clipboard, so none here means iOS
      // was told not to allow the paste, or could not fetch it.
      if (!image) {
        setError("Couldn't paste that photo. Copy it again, then try.");
        return;
      }
      const file = new File(Paths.cache, `pasted-${Date.now()}.jpg`);
      file.write(image.data.slice(image.data.indexOf(",") + 1), { encoding: "base64" });
      await stagePhoto({
        uri: file.uri,
        fileName: "Pasted photo.jpg",
        mimeType: "image/jpeg",
        fileSize: file.size ?? 0,
        width: image.size.width,
        height: image.size.height,
      });
    } catch {
      setError("Couldn't paste that photo. Copy it again, then try.");
    } finally {
      setPreparing(false);
    }
  }, [stagePhoto]);

  const pasteIntoField = useCallback(
    async (payload: PasteEventPayload) => {
      if (payload.type !== "images" || directPasteInFlight.current) return;
      const remaining = Math.max(0, MAX_PHOTOS - draft.attachments.length);
      if (remaining === 0) return;
      directPasteInFlight.current = true;
      setPreparing(true);
      try {
        for (const uri of payload.uris.slice(0, remaining)) {
          const file = new File(uri);
          await stagePhoto({
            uri: file.uri,
            fileName: file.name,
            mimeType: undefined,
            fileSize: file.size ?? 0,
            width: 0,
            height: 0,
          });
        }
      } catch {
        setError("Could not paste that photo. Copy it again, then try.");
      } finally {
        directPasteInFlight.current = false;
        setPreparing(false);
      }
    },
    [draft.attachments.length, stagePhoto],
  );

  const attach = useCallback(async () => {
    setError(null);
    // Offered only when there is a photo to paste. Asking whether there is
    // one does not raise iOS's paste prompt; reading it does.
    const canPaste = await Clipboard.hasImageAsync().catch(() => false);
    const choices = [
      ...(canPaste ? [{ text: "Paste Photo", onPress: () => void pastePhoto() }] : []),
      { text: "Photo Library", onPress: () => void pickFromLibrary() },
      { text: "Take Photo", onPress: () => void takePhoto() },
    ];
    if (Platform.OS === "ios") {
      ActionSheetIOS.showActionSheetWithOptions(
        { options: [...choices.map((choice) => choice.text), "Cancel"], cancelButtonIndex: choices.length },
        (index) => choices[index]?.onPress(),
      );
      return;
    }
    Alert.alert("Attach photo", undefined, [...choices, { text: "Cancel", style: "cancel" }]);
  }, [pastePhoto, pickFromLibrary, takePhoto]);

  const body = text.trim();
  const mentions = resolveTypedMentions(body, members);
  const { ids: sessionMentions, unresolved: staleHandles } = resolveTypedSessionMentions(body, sessionTargets.map(({ candidate }) => candidate), selectedSessions);
  const staleKey = staleHandles.join(" ");
  // The web composer's three limits, verbatim — the server enforces all of
  // them, and being told before you send beats a rejection after.
  const limit =
    new TextEncoder().encode(text).length > 100_000
      ? "Keep your message within 100,000 UTF-8 bytes."
      : mentions.length > 32
        ? "Mention at most 32 room members."
        : sessionMentions.length > 4
          ? "Mention at most four sessions."
        : null;
  // Who this message will actually reach, by the same rule the backend
  // dispatches on — so the answer is not a guess the two could disagree about.
  const hint = respondHint(roomKind, members, mentions, runningAgentIds ?? [], true);
  const hasContent = !!body || draft.attachments.length > 0;
  // Photos upload as they are picked, so sending waits only on the ones still
  // in flight — and a failed one has to be retried or removed first, rather
  // than silently dropping out of the message.
  const busy = sending || draft.uploading || preparing;
  const submit = useCallback(async (sendStale = false) => {
    if (!hasContent || busy || disabled || limit || draft.failed) return;
    // A handle that stopped matching would otherwise go out as plain text without a word.
    if (staleKey && !sendStale) {
      setStaleNotice(staleKey);
      return;
    }
    setStaleNotice("");
    setSending(true);
    onTypingChange?.(false);
    setError(null);
    const message: ComposedMessage = {
      text: body,
      mentions,
      ...(sessionMentions.length ? { sessionMentions } : {}),
      attachmentIds: draft.attachmentIds,
    };
    draft.clear();
    setSelectedSessions({});
    setCaret(null);
    // A sent message ends the formatting session; the next one starts clean.
    setFormatOpen(false);
    try {
      await onSend(message);
    } finally {
      setSending(false);
    }
  }, [hasContent, busy, disabled, limit, staleKey, body, mentions, sessionMentions, draft, onSend, onTypingChange]);

  return (
    <View
      testID="composer-outer"
      style={[
        styles.outer,
        { paddingBottom: Math.max(insets.bottom, spacing.sm), backgroundColor: palette.ground },
      ]}
    >
      {reconnecting && <ReconnectingBanner palette={palette} />}
      {emojiOptions.length > 0 && (
        // A strip above the card, not an overlay: the keyboard owns the bottom
        // of the screen and a popover down there would sit under it.
        <ScrollView
          horizontal
          keyboardShouldPersistTaps="always"
          showsHorizontalScrollIndicator={false}
          contentContainerStyle={styles.strip}
        >
          {emojiOptions.map((entry) => (
            <Pressable
              key={entry.emoji}
              style={[styles.option, { backgroundColor: palette.surface, borderColor: palette.edge }]}
              onPress={() => complete(entry.emoji)}
              accessibilityRole="button"
              accessibilityLabel={`Insert ${entry.emoji}`}
            >
              <Text style={styles.glyph}>{entry.emoji}</Text>
              <Text style={[styles.optionName, { color: palette.fg }]} numberOfLines={1}>
                {entry.name}
              </Text>
            </Pressable>
          ))}
        </ScrollView>
      )}
      {(error ?? draft.uploadError ?? limit) != null && (
        <Text style={[styles.error, { color: palette.alert }]}>
          {error ?? draft.uploadError ?? limit}
        </Text>
      )}
      {staleKey && staleNotice === staleKey ? (
        <>
          <Text style={[styles.error, { color: palette.alert }]} accessibilityRole="alert">
            {staleHandles.map((handle) => `@${handle}`).join(", ")} no longer {staleHandles.length === 1 ? "matches" : "match"} a shared session. Remove it or send anyway.
          </Text>
          <Text style={[styles.error, { color: palette.alert, textDecorationLine: "underline" }]} accessibilityRole="button" onPress={() => void submit(true)}>
            Send anyway
          </Text>
        </>
      ) : null}
      <AttachmentChips
        attachments={draft.attachments}
        onRemove={draft.remove}
        onRetry={draft.retry}
      />
      {/* Who will answer — its own line above the field, not the rail's empty
          middle: the rail is gone now that Formatting and emoji moved into
          the pill with Send. */}
      {hint ? (
        <Text
          style={[styles.answerHint, { color: palette.fgMute }]}
          maxFontSizeMultiplier={1.4}
          numberOfLines={1}
        >
          {hint}
        </Text>
      ) : null}
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
                tintColor={palette.fgMute}
              />
            </Pressable>
          ))}
        </ScrollView>
      )}
      <View style={styles.inputRow}>
        {/* The "+" IS the attach button, relocated: same handler, label,
            disabled rule and uploading spinner as before. */}
        <Pressable
          testID="composer-plus"
          style={[styles.plusButton, { backgroundColor: palette.chip }]}
          onPress={() => void attach()}
          disabled={disabled || busy || draft.attachments.length >= MAX_PHOTOS}
          hitSlop={6}
          accessibilityRole="button"
          accessibilityLabel="Attach photo"
        >
          {draft.uploading ? (
            <ActivityIndicator size="small" color={palette.fgSoft} />
          ) : (
            <SymbolView accessibilityElementsHidden name="plus" size={16} tintColor={palette.fgSoft} />
          )}
        </Pressable>
        <Reanimated.View
          testID="composer-pill"
          style={[styles.pill, { backgroundColor: palette.surface }, pillStyle]}
        >
          <TextInputWrapper style={styles.inputWrapper} onPaste={(payload) => void pasteIntoField(payload)}>
            <TextInput
              style={[
                styles.input,
                { color: palette.fg, minHeight: inputMinHeight, maxHeight: inputMaxHeight },
              ]}
              placeholder={placeholder}
              placeholderTextColor={palette.fgMute}
              value={text}
              onChangeText={(next) => {
                setText(next);
                onTypingChange?.(!!next.trim());
              }}
              onSelectionChange={onSelectionChange}
              selection={pendingSelection ?? undefined}
              multiline
              // Editable through a send: a field that stops being editable gives up
              // the keyboard, which closed it after every message. `busy` already
              // keeps a second send from starting.
              editable={!disabled}
              onFocus={() => {
                focusProgress.value = withSpring(1, FOCUS_SPRING);
              }}
              onBlur={() => {
                focusProgress.value = withSpring(0, FOCUS_SPRING);
                onTypingChange?.(false);
              }}
            />
          </TextInputWrapper>
          <Pressable
            style={styles.pillIcon}
            onPress={() => {
              hapticSelection();
              setFormatOpen((open) => !open);
            }}
            disabled={disabled || sending}
            hitSlop={8}
            accessibilityRole="button"
            accessibilityLabel="Formatting"
            accessibilityState={{ expanded: formatOpen }}
          >
            <SymbolView
              accessibilityElementsHidden
              name="textformat"
              size={18}
              tintColor={formatOpen ? palette.fg : palette.fgMute}
            />
          </Pressable>
          <Pressable
            style={styles.pillIcon}
            onPress={() => setEmojiOpen(true)}
            disabled={disabled || sending}
            hitSlop={8}
            accessibilityRole="button"
            accessibilityLabel="Insert emoji"
          >
            <SymbolView
              accessibilityElementsHidden
              name="face.smiling"
              size={18}
              tintColor={palette.fgMute}
            />
          </Pressable>
        </Reanimated.View>
        {onAbort && !hasContent ? (
          <Pressable
            style={[styles.sendButton, { backgroundColor: palette.fg }]}
            onPress={onAbort}
            hitSlop={6}
            accessibilityRole="button"
            accessibilityLabel="Stop"
          >
            <SymbolView accessibilityElementsHidden name="stop.fill" size={14} tintColor={palette.onFg} />
          </Pressable>
        ) : (
          <AnimatedPressable
            style={[
              styles.sendButton,
              { backgroundColor: palette.fg },
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
            hitSlop={6}
            accessibilityRole="button"
            accessibilityLabel="Send"
          >
            {sending ? (
              <ActivityIndicator size="small" color={palette.onFg} />
            ) : (
              <SymbolView accessibilityElementsHidden name="arrow.up" size={16} tintColor={palette.onFg} />
            )}
          </AnimatedPressable>
        )}
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
    </View>
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
  // Its own line above the field now that the rail is gone — left-aligned,
  // the last thing read before sending.
  answerHint: {
    fontFamily: fonts.sans,
    fontSize: 12,
    lineHeight: 16,
    textAlign: "left",
    paddingHorizontal: spacing.lg,
    paddingTop: spacing.xs,
  },
  // No line against the timeline and no bar: the field sits on the page ground
  // on its own, the way Messages draws it. `backgroundColor` is applied inline
  // (`palette.ground`).
  outer: {
    paddingHorizontal: spacing.sm,
    paddingTop: spacing.xs,
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
    borderRadius: radii.lg,
    borderWidth: 1,
    paddingHorizontal: spacing.md,
    paddingVertical: spacing.sm,
  },
  optionLabels: { flexShrink: 1 },
  optionName: {
    fontFamily: fonts.sansMedium,
    fontSize: 13,
    lineHeight: 18,
    flexShrink: 1,
  },
  optionMeta: {
    fontFamily: fonts.sans,
    fontSize: 11,
    lineHeight: 15,
  },
  glyph: { fontSize: 18, lineHeight: 22 },
  error: {
    fontFamily: fonts.sansMedium,
    fontSize: 12,
    lineHeight: 16,
    paddingHorizontal: spacing.lg,
    paddingTop: spacing.md,
  },
  // 22pt of leading in a 28pt row, the icons' height, so one line of text and
  // the icons beside it share a centre.
  inputWrapper: { flex: 1 },
  // No flex here: the wrapper's height comes from this field, so a flexed field
  // took the wrapper's one-line height and never grew past it.
  input: {
    fontFamily: fonts.sans,
    fontSize: 16,
    lineHeight: 22,
    marginVertical: 3,
    paddingTop: 0,
    paddingBottom: 0,
    textAlignVertical: "top",
  },
  // "+" · pill · send, bottom-aligned so the pill can grow upward with the text.
  inputRow: {
    flexDirection: "row",
    alignItems: "flex-end",
    gap: spacing.sm,
    paddingHorizontal: spacing.sm,
    paddingTop: 6,
    paddingBottom: spacing.sm,
  },
  // 36pt beside a 40pt field: the 2pt under it centres the two on one line.
  plusButton: {
    width: 36,
    height: 36,
    borderRadius: 18,
    marginBottom: 2,
    alignItems: "center",
    justifyContent: "center",
  },
  // The field itself: a capsule 40pt tall on one line (border, 5, the 28pt row,
  // 5, border) that grows upward. Formatting and emoji sit inside it,
  // bottom-aligned beside the text.
  pill: {
    flex: 1,
    flexDirection: "row",
    alignItems: "flex-end",
    borderRadius: 20,
    borderWidth: 1,
    paddingLeft: 14,
    paddingRight: 6,
    paddingVertical: 5,
  },
  pillIcon: {
    width: 28,
    height: 28,
    borderRadius: 14,
    alignItems: "center",
    justifyContent: "center",
  },
  sendButton: {
    width: 36,
    height: 36,
    borderRadius: 18,
    marginBottom: 2,
    alignItems: "center",
    justifyContent: "center",
  },
  sendDisabled: { opacity: 0.4 },
  banner: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.sm,
    borderWidth: 1,
    borderRadius: radii.md,
    paddingHorizontal: spacing.md,
    paddingVertical: 6,
    marginBottom: spacing.sm,
  },
  bannerDot: { width: 6, height: 6, borderRadius: 3 },
  bannerText: {
    flex: 1,
    fontFamily: fonts.sansMedium,
    fontSize: 12,
    lineHeight: 16,
  },
});
