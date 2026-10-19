import { SymbolView } from "../ui/SymbolView";
import { Image } from "expo-image";
import { ActivityIndicator, Pressable, StyleSheet, Text, View } from "react-native";

import { fonts, radii, spacing, useChatPalette } from "../../config/theme";
import type { StagedAttachment } from "../../lib/composerDraft";

/** Web ChatInput's formatFileSize, verbatim. */
function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes}B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)}KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)}MB`;
}

/**
 * Attachment chips above the composer input — the web's PendingFile row (file
 * icon · name · size · remove ×) at mobile scale.
 *
 * Each photo uploads on its own as it is picked, so each chip carries its own
 * state: a spinner while it is in flight, a Retry while it has failed, its
 * size once it has landed. One failure is the reader's to retry or remove, and
 * it never takes the rest of the message with it.
 */
export function AttachmentChips({
  attachments,
  onRemove,
  onRetry,
}: {
  attachments: readonly StagedAttachment[];
  onRemove: (key: string) => void;
  onRetry: (key: string) => void;
}) {
  const palette = useChatPalette();
  if (attachments.length === 0) return null;
  return (
    <View style={styles.row}>
      {attachments.map((a) => {
        const uploading = !a.attachmentId && !a.failed;
        return (
          <View
            key={a.key}
            style={[
              styles.chip,
              { backgroundColor: palette.chip, borderColor: a.failed ? palette.alert : palette.chip },
            ]}
          >
            {a.contentType.startsWith("image/") ? (
              <Image
                source={{ uri: a.uri }}
                style={[
                  styles.preview,
                  { backgroundColor: palette.edge },
                  uploading && styles.previewUploading,
                ]}
                contentFit="cover"
                accessibilityLabel={`Preview ${a.name}`}
              />
            ) : (
              <SymbolView accessibilityElementsHidden name="doc" size={12} tintColor={palette.fgMute} />
            )}
            <Text style={[styles.name, { color: palette.fg }]} numberOfLines={1}>
              {a.name}
            </Text>
            {uploading ? (
              <ActivityIndicator size="small" color={palette.fgMute} accessibilityLabel="Uploading" />
            ) : a.failed ? (
              <Pressable
                onPress={() => onRetry(a.key)}
                hitSlop={8}
                accessibilityRole="button"
                accessibilityLabel={`Retry uploading ${a.name}`}
              >
                <Text style={[styles.retry, { color: palette.alert }]} maxFontSizeMultiplier={1.4}>
                  Retry
                </Text>
              </Pressable>
            ) : (
              a.size > 0 && (
                <Text style={[styles.size, { color: palette.fgMute }]}>{formatFileSize(a.size)}</Text>
              )
            )}
            <Pressable
              onPress={() => onRemove(a.key)}
              hitSlop={8}
              accessibilityRole="button"
              accessibilityLabel={`Remove ${a.name}`}
            >
              <SymbolView accessibilityElementsHidden name="xmark" size={12} tintColor={palette.fgMute} />
            </Pressable>
          </View>
        );
      })}
    </View>
  );
}

const styles = StyleSheet.create({
  row: {
    flexDirection: "row",
    flexWrap: "wrap",
    gap: spacing.sm,
    paddingHorizontal: spacing.lg,
    paddingTop: spacing.md,
  },
  // The chip step of the conversation's own palette, so it is not a white
  // card in dark mode. A failed upload reads as failed without relying on
  // colour alone: its border turns and its size becomes the word "Retry".
  chip: {
    flexDirection: "row",
    alignItems: "center",
    gap: 6,
    borderWidth: StyleSheet.hairlineWidth,
    borderRadius: radii.lg,
    paddingHorizontal: 10,
    paddingVertical: 6,
    maxWidth: "80%",
  },
  retry: {
    fontFamily: fonts.sansMedium,
    fontSize: 12,
    lineHeight: 16,
  },
  previewUploading: { opacity: 0.5 },
  preview: {
    width: 40,
    height: 40,
    borderRadius: radii.md,
  },
  name: {
    fontFamily: fonts.sans,
    fontSize: 12,
    lineHeight: 16,
    flexShrink: 1,
  },
  size: {
    fontFamily: fonts.sans,
    fontSize: 12,
    lineHeight: 16,
  },
});
