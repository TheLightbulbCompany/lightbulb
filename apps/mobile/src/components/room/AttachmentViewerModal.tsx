import { SymbolView } from "expo-symbols";
import type { File } from "expo-file-system";
import { Image } from "expo-image";
import * as Sharing from "expo-sharing";
import { StatusBar } from "expo-status-bar";
import { useEffect, useRef, useState } from "react";
import {
  ActivityIndicator,
  Modal,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  View,
  type GestureResponderEvent,
  useWindowDimensions,
} from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import { WebView } from "react-native-webview";
import { isVideoAttachmentType, type MessageAttachment } from "@lightbulb/shared";

import { colors, fonts, radii, spacing } from "../../config/theme";
import { attachmentFile } from "../../lib/attachmentFile";

const isImageType = (contentType: string) => contentType.startsWith("image/");
const isPdfType = (contentType: string) => contentType === "application/pdf";

/** Same as AttachmentChips' formatFileSize (web ChatInput's, verbatim). */
function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/**
 * Pinch + double-tap zoom on a plain ScrollView — iOS gives pinch-zoom for
 * free via `maximumZoomScale`/`minimumZoomScale`/`centerContent`; double-tap
 * is a few lines of manual tap-timing plus the ScrollView's own (typed, if
 * undocumented-as-a-prop) `scrollResponderZoomTo` imperative method, so
 * neither needs react-native-gesture-handler or reanimated.
 */
function ZoomableImage({ uri, label }: { uri: string; label: string }) {
  const { width, height } = useWindowDimensions();
  const scrollRef = useRef<ScrollView>(null);
  const zoomedRef = useRef(false);
  const lastTapAt = useRef(0);

  const handleTap = (event: GestureResponderEvent) => {
    const now = Date.now();
    const isDoubleTap = now - lastTapAt.current < 300;
    lastTapAt.current = now;
    if (!isDoubleTap) return;
    const zoomed = zoomedRef.current;
    zoomedRef.current = !zoomed;
    if (zoomed) {
      scrollRef.current?.scrollResponderZoomTo({ x: 0, y: 0, width, height, animated: true });
      return;
    }
    const { locationX, locationY } = event.nativeEvent;
    const zoomWidth = width / 2.5;
    const zoomHeight = height / 2.5;
    scrollRef.current?.scrollResponderZoomTo({
      x: Math.max(0, locationX - zoomWidth / 2),
      y: Math.max(0, locationY - zoomHeight / 2),
      width: zoomWidth,
      height: zoomHeight,
      animated: true,
    });
  };

  return (
    <ScrollView
      ref={scrollRef}
      style={styles.fill}
      contentContainerStyle={styles.zoomContent}
      maximumZoomScale={4}
      minimumZoomScale={1}
      centerContent
      showsHorizontalScrollIndicator={false}
      showsVerticalScrollIndicator={false}
    >
      <Pressable onPress={handleTap} accessibilityLabel={label} accessibilityRole="image">
        <Image source={{ uri }} style={{ width, height }} contentFit="contain" allowDownscaling={false} />
      </Pressable>
    </ScrollView>
  );
}

/** The "anything else" case: no player, just what it is and a way out to iOS. */
function AttachmentFallbackCard({
  attachment,
  onOpen,
  busy,
}: {
  attachment: MessageAttachment;
  onOpen: () => void;
  busy: boolean;
}) {
  return (
    <View style={styles.center}>
      <View style={styles.card}>
        <SymbolView accessibilityElementsHidden name="doc" size={40} tintColor={colors.cream} />
        <Text style={styles.cardName} numberOfLines={2}>
          {attachment.fileName}
        </Text>
        <Text style={styles.cardMeta}>
          {formatFileSize(attachment.size)} · {attachment.contentType}
        </Text>
        <Pressable
          onPress={onOpen}
          disabled={busy}
          accessibilityRole="button"
          accessibilityLabel={`Open ${attachment.fileName} in another app`}
          style={({ pressed }) => [styles.cardButton, pressed && styles.cardButtonPressed]}
        >
          {busy ? (
            <ActivityIndicator size="small" color={colors.forest} />
          ) : (
            <SymbolView accessibilityElementsHidden name="square.and.arrow.up" size={16} tintColor={colors.forest} />
          )}
          <Text style={styles.cardButtonText}>Open</Text>
        </Pressable>
      </View>
    </View>
  );
}

/** One page of the viewer: downloads the current attachment, then renders it
 * per `contentType` — an image, a rendered PDF, or the fallback card. */
function AttachmentPage({
  roomId,
  attachment,
  attachmentUrl,
  onShare,
  sharing,
}: {
  roomId: string;
  attachment: MessageAttachment;
  attachmentUrl: (roomId: string, attachmentId: string) => Promise<{ url: string; expiresIn: number }>;
  onShare: () => void;
  sharing: boolean;
}) {
  const isVideo = isVideoAttachmentType(attachment.contentType);
  const [file, setFile] = useState<File | null>(null);
  // A video plays straight off its presigned URL — WKWebView handles a direct
  // mp4/mov URL natively, and there's no reason to round-trip it to disk first.
  const [videoUri, setVideoUri] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setFile(null);
    setVideoUri(null);
    setFailed(false);
    const load = isVideo
      ? attachmentUrl(roomId, attachment.id).then(({ url }) => {
          if (!cancelled) setVideoUri(url);
        })
      : attachmentFile(roomId, attachment, attachmentUrl).then((next) => {
          if (!cancelled) setFile(next);
        });
    load.catch(() => {
      if (!cancelled) setFailed(true);
    });
    return () => {
      cancelled = true;
    };
  }, [roomId, attachment, attachmentUrl, isVideo]);

  if (failed) {
    return (
      <View style={styles.center}>
        <Text style={styles.errorText}>Could not load {attachment.fileName}</Text>
      </View>
    );
  }
  if (isVideo) {
    if (!videoUri) {
      return (
        <View style={styles.center}>
          <ActivityIndicator color={colors.cream} />
        </View>
      );
    }
    return (
      <WebView
        key={attachment.id}
        source={{ uri: videoUri }}
        style={styles.fill}
        allowsInlineMediaPlayback
        mediaPlaybackRequiresUserAction={false}
      />
    );
  }
  if (!file) {
    return (
      <View style={styles.center}>
        <ActivityIndicator color={colors.cream} />
      </View>
    );
  }
  // Keyed by attachment id so paging to a different attachment remounts
  // fresh — otherwise the ScrollView/WebView instance (same component type,
  // same tree position) would carry the previous page's zoom/scroll state.
  if (isImageType(attachment.contentType)) {
    return <ZoomableImage key={attachment.id} uri={file.uri} label={attachment.fileName} />;
  }
  if (isPdfType(attachment.contentType)) {
    return (
      <WebView
        key={attachment.id}
        source={{ uri: file.uri }}
        style={styles.fill}
        originWhitelist={["*"]}
      />
    );
  }
  return (
    <AttachmentFallbackCard
      key={attachment.id}
      attachment={attachment}
      onOpen={onShare}
      busy={sharing}
    />
  );
}

/**
 * Full-screen attachment viewer. Mounted conditionally by its caller (the
 * same convention as `MessageActionsSheet`: `{viewerIndex !== null && <...>}`),
 * so there is no slide/fade-out on close — it appears and vanishes.
 */
export function AttachmentViewerModal({
  attachments,
  initialIndex,
  roomId,
  attachmentUrl,
  onClose,
}: {
  attachments: readonly MessageAttachment[];
  initialIndex: number;
  roomId: string;
  attachmentUrl: (roomId: string, attachmentId: string) => Promise<{ url: string; expiresIn: number }>;
  onClose: () => void;
}) {
  const insets = useSafeAreaInsets();
  const [index, setIndex] = useState(() =>
    Math.min(Math.max(initialIndex, 0), Math.max(attachments.length - 1, 0)),
  );
  const [sharing, setSharing] = useState(false);
  const current = attachments[index];

  const handleShare = async () => {
    if (!current || sharing) return;
    setSharing(true);
    try {
      const file = await attachmentFile(roomId, current, attachmentUrl);
      if (await Sharing.isAvailableAsync()) {
        await Sharing.shareAsync(file.uri, {
          mimeType: current.contentType,
          dialogTitle: current.fileName,
        });
      }
    } catch {
      // ponytail: silent failure here — a persistent error banner for a share
      // sheet nobody has asked for yet. The user can just try again.
    } finally {
      setSharing(false);
    }
  };

  if (!current) return null;
  const canPrev = index > 0;
  const canNext = index < attachments.length - 1;

  return (
    <Modal visible transparent={false} animationType="fade" onRequestClose={onClose} statusBarTranslucent>
      {/* The viewer is dark in either scheme, so its clock and battery are light. */}
      <StatusBar style="light" />
      <View style={styles.root}>
        <View style={[styles.topBar, { paddingTop: insets.top + spacing.sm }]}>
          <Text style={styles.fileName} numberOfLines={1}>
            {current.fileName}
          </Text>
          <View style={styles.topBarActions}>
            <Pressable
              onPress={handleShare}
              disabled={sharing}
              accessibilityRole="button"
              accessibilityLabel={`Share ${current.fileName}`}
              style={styles.iconButton}
            >
              {sharing ? (
                <ActivityIndicator size="small" color={colors.cream} />
              ) : (
                <SymbolView accessibilityElementsHidden name="square.and.arrow.up" size={20} tintColor={colors.cream} />
              )}
            </Pressable>
            <Pressable
              onPress={onClose}
              accessibilityRole="button"
              accessibilityLabel="Close"
              style={styles.iconButton}
            >
              <SymbolView accessibilityElementsHidden name="xmark" size={22} tintColor={colors.cream} />
            </Pressable>
          </View>
        </View>

        <AttachmentPage
          roomId={roomId}
          attachment={current}
          attachmentUrl={attachmentUrl}
          onShare={handleShare}
          sharing={sharing}
        />

        {attachments.length > 1 ? (
          <View style={[styles.pager, { paddingBottom: insets.bottom + spacing.sm }]}>
            <Pressable
              onPress={() => setIndex((i) => Math.max(0, i - 1))}
              disabled={!canPrev}
              accessibilityRole="button"
              accessibilityLabel="Previous attachment"
              style={[styles.iconButton, !canPrev && styles.iconButtonDisabled]}
            >
              <SymbolView accessibilityElementsHidden name="chevron.left" size={22} tintColor={colors.cream} />
            </Pressable>
            <Text style={styles.pagerLabel}>
              {index + 1} of {attachments.length}
            </Text>
            <Pressable
              onPress={() => setIndex((i) => Math.min(attachments.length - 1, i + 1))}
              disabled={!canNext}
              accessibilityRole="button"
              accessibilityLabel="Next attachment"
              style={[styles.iconButton, !canNext && styles.iconButtonDisabled]}
            >
              <SymbolView accessibilityElementsHidden name="chevron.right" size={22} tintColor={colors.cream} />
            </Pressable>
          </View>
        ) : null}
      </View>
    </Modal>
  );
}

const styles = StyleSheet.create({
  // The manual has no midnight-surface tokens yet (theme.ts: "the app is
  // cream-only"), so this reuses `forest`/`cream` as an inverted pair — they
  // are exact photographic negatives of each other (#242428 / #ffffff), which
  // is what a dimmed full-screen backdrop needs. A dedicated on-dark text
  // token can replace this when a broader dark-surface UI ships.
  root: { flex: 1, backgroundColor: colors.forest },
  fill: { flex: 1 },
  center: { flex: 1, alignItems: "center", justifyContent: "center", padding: spacing.xl },
  zoomContent: { flexGrow: 1, alignItems: "center", justifyContent: "center" },
  topBar: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    gap: spacing.md,
    paddingHorizontal: spacing.lg,
    paddingBottom: spacing.sm,
  },
  topBarActions: { flexDirection: "row", alignItems: "center", gap: spacing.xs },
  fileName: {
    flex: 1,
    fontFamily: fonts.sansMedium,
    fontSize: 15,
    lineHeight: 20,
    color: colors.cream,
  },
  iconButton: {
    width: 44,
    height: 44,
    alignItems: "center",
    justifyContent: "center",
  },
  iconButtonDisabled: { opacity: 0.3 },
  pager: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "center",
    gap: spacing.lg,
  },
  pagerLabel: {
    fontFamily: fonts.sans,
    fontSize: 13,
    lineHeight: 18,
    color: colors.creamDeeper,
    minWidth: 64,
    textAlign: "center",
  },
  errorText: {
    fontFamily: fonts.sans,
    fontSize: 14,
    lineHeight: 20,
    color: colors.creamDeeper,
    textAlign: "center",
  },
  card: {
    alignItems: "center",
    gap: spacing.sm,
    maxWidth: "100%",
  },
  cardName: {
    fontFamily: fonts.sansMedium,
    fontSize: 16,
    lineHeight: 22,
    color: colors.cream,
    textAlign: "center",
  },
  cardMeta: {
    fontFamily: fonts.sans,
    fontSize: 13,
    lineHeight: 18,
    color: colors.creamDeeper,
    textAlign: "center",
  },
  cardButton: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.xs,
    marginTop: spacing.md,
    minHeight: 44,
    paddingHorizontal: spacing.lg,
    borderRadius: radii.xl,
    backgroundColor: colors.cream,
  },
  cardButtonPressed: { backgroundColor: colors.creamDeeper },
  cardButtonText: { fontFamily: fonts.sansMedium, fontSize: 15, lineHeight: 20, color: colors.forest },
});
