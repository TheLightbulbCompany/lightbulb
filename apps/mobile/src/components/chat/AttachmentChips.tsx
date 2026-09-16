import { SymbolView } from "expo-symbols";
import { Image } from "expo-image";
import { ActivityIndicator, Pressable, StyleSheet, Text, View } from "react-native";

import { colors, fonts, radii, spacing } from "../../config/theme";
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
  if (attachments.length === 0) return null;
  return (
    <View style={styles.row}>
      {attachments.map((a) => {
        const uploading = !a.attachmentId && !a.failed;
        return (
          <View key={a.key} style={[styles.chip, a.failed && styles.chipFailed]}>
            {a.contentType.startsWith("image/") ? (
              <Image
                source={{ uri: a.uri }}
                style={[styles.preview, uploading && styles.previewUploading]}
                contentFit="cover"
                accessibilityLabel={`Preview ${a.name}`}
              />
            ) : (
              <SymbolView accessibilityElementsHidden name="doc" size={12} tintColor={colors.mute} />
            )}
            <Text style={styles.name} numberOfLines={1}>
              {a.name}
            </Text>
            {uploading ? (
              <ActivityIndicator size="small" color={colors.mute} accessibilityLabel="Uploading" />
            ) : a.failed ? (
              <Pressable
                onPress={() => onRetry(a.key)}
                hitSlop={8}
                accessibilityRole="button"
                accessibilityLabel={`Retry uploading ${a.name}`}
              >
                <Text style={styles.retry} maxFontSizeMultiplier={1.4}>
                  Retry
                </Text>
              </Pressable>
            ) : (
              a.size > 0 && <Text style={styles.size}>{formatFileSize(a.size)}</Text>
            )}
            <Pressable
              onPress={() => onRemove(a.key)}
              hitSlop={8}
              accessibilityRole="button"
              accessibilityLabel={`Remove ${a.name}`}
            >
              <SymbolView accessibilityElementsHidden name="xmark" size={12} tintColor={colors.placeholder} />
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
  // Web chip: the one surface step (chip token), rounded-lg, tiny type.
  chip: {
    flexDirection: "row",
    alignItems: "center",
    gap: 6,
    backgroundColor: colors.chip,
    borderRadius: radii.lg,
    paddingHorizontal: 10,
    paddingVertical: 6,
    maxWidth: "80%",
  },
  // A failed upload reads as failed without relying on colour alone — the
  // chip also swaps its size for the word "Retry".
  chipFailed: { backgroundColor: colors.errorBg },
  retry: {
    fontFamily: fonts.sansMedium,
    fontSize: 12,
    lineHeight: 16,
    color: colors.errorText,
  },
  previewUploading: { opacity: 0.5 },
  preview: {
    width: 40,
    height: 40,
    borderRadius: radii.md,
    backgroundColor: colors.edge,
  },
  name: {
    fontFamily: fonts.sans,
    fontSize: 12,
    lineHeight: 16,
    color: colors.avatarText,
    flexShrink: 1,
  },
  size: {
    fontFamily: fonts.sans,
    fontSize: 12,
    lineHeight: 16,
    color: colors.placeholder,
  },
});
