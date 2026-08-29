import Feather from "@expo/vector-icons/Feather";
import { Pressable, StyleSheet, Text, View } from "react-native";

import { colors, fonts, radii, spacing } from "../../config/theme";
import type { PendingAttachment } from "../../lib/attachments";

/** Web ChatInput's formatFileSize, verbatim. */
function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes}B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)}KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)}MB`;
}

/**
 * Pending-attachment chips above the composer input — the web's PendingFile
 * row (file icon · name · size · remove ×) at mobile scale. Purely
 * presentational; the screen owns the pending list.
 */
export function AttachmentChips({
  attachments,
  onRemove,
}: {
  attachments: PendingAttachment[];
  onRemove: (id: string) => void;
}) {
  if (attachments.length === 0) return null;
  return (
    <View style={styles.row}>
      {attachments.map((a) => (
        <View key={a.id} style={styles.chip}>
          <Feather name="file" size={12} color={colors.mute} />
          <Text style={styles.name} numberOfLines={1}>
            {a.name}
          </Text>
          {a.size > 0 && <Text style={styles.size}>{formatFileSize(a.size)}</Text>}
          <Pressable
            onPress={() => onRemove(a.id)}
            hitSlop={8}
            accessibilityRole="button"
            accessibilityLabel={`Remove ${a.name}`}
          >
            <Feather name="x" size={12} color={colors.placeholder} />
          </Pressable>
        </View>
      ))}
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
