import type { ReactNode } from "react";
import { Pressable, StyleSheet, Text, View, type PressableProps } from "react-native";

import { fonts, spacing, type ChatPalette } from "../../config/theme";

export interface ListRowProps {
  palette: ChatPalette;
  /** An Avatar (52pt Chats, 36pt Activity). */
  leading: ReactNode;
  title: string;
  /** Small words after the title: "Draft", "Shared", a bell.slash glyph. */
  titleAccessory?: ReactNode;
  time?: string;
  preview?: string | null;
  /** "live" = link colour (typing… / working…). */
  previewTone?: "normal" | "live";
  previewLines?: 1 | 2;
  /** Third line under the preview, 1 line, fgMute (Activity's snippet). */
  detail?: string | null;
  /** Blue dot in the left gutter + semibold title. */
  unread?: boolean;
  /** Tinted `palette.selected` background (e.g. the row a notification opened). */
  highlighted?: boolean;
  /** Part 3: Activity's one action button. */
  trailing?: ReactNode;
  /** Link.Trigger injects this. */
  onPress?: () => void;
  onLongPress?: () => void;
  testID?: string;
  accessibilityLabel: string;
  accessibilityHint?: string;
  accessibilityActions?: PressableProps["accessibilityActions"];
  onAccessibilityAction?: PressableProps["onAccessibilityAction"];
}

/**
 * The one list-row anatomy shared by Chats, Activity and Settings: a leading
 * slot, a title line (bold + gutter dot when unread) with an optional small
 * accessory and a trailing time, and an optional one/two-line preview under
 * it. `trailing` sits OUTSIDE the Pressable so VoiceOver reaches it as its
 * own element instead of it being swallowed into the row's label.
 */
export function ListRow({
  palette,
  leading,
  title,
  titleAccessory,
  time,
  preview,
  previewTone = "normal",
  previewLines = 2,
  detail,
  unread = false,
  highlighted = false,
  trailing,
  onPress,
  onLongPress,
  testID,
  accessibilityLabel,
  accessibilityHint,
  accessibilityActions,
  onAccessibilityAction,
}: ListRowProps) {
  const rowContent = (
    <>
      {unread ? (
        <View
          accessible
          accessibilityLabel="Unread"
          style={[styles.dot, { backgroundColor: palette.link }]}
        />
      ) : null}
      {leading}
      <View style={[styles.textColumn, { borderBottomColor: palette.edge }]}>
        <View style={styles.firstLine}>
          <Text
            style={[styles.title, { color: palette.fg }, unread ? styles.titleUnread : null]}
            numberOfLines={1}
          >
            {title}
          </Text>
          {titleAccessory}
          {time ? <Text style={[styles.time, { color: palette.fgMute }]}>{time}</Text> : null}
        </View>
        {preview != null ? (
          <Text
            style={[
              styles.preview,
              { color: previewTone === "live" ? palette.link : palette.fgMute },
            ]}
            numberOfLines={previewLines}
          >
            {preview}
          </Text>
        ) : null}
        {detail != null ? (
          <Text style={[styles.detail, { color: palette.fgMute }]} numberOfLines={1}>
            {detail}
          </Text>
        ) : null}
      </View>
    </>
  );
  // No onPress/onLongPress (a resolved item, or an open one with neither a
  // navigation target nor review actions): render inert, not a button that
  // does nothing — no accessibilityRole="button", no pressed feedback.
  const interactive = onPress != null || onLongPress != null;
  return (
    <View style={styles.outer}>
      {interactive ? (
        <Pressable
          testID={testID}
          onPress={onPress}
          onLongPress={onLongPress}
          accessibilityRole="button"
          accessibilityLabel={accessibilityLabel}
          accessibilityHint={accessibilityHint}
          accessibilityActions={accessibilityActions}
          onAccessibilityAction={onAccessibilityAction}
          style={({ pressed }) => [
            styles.row,
            { backgroundColor: pressed || highlighted ? palette.selected : palette.surface },
          ]}
        >
          {rowContent}
        </Pressable>
      ) : (
        <View
          testID={testID}
          accessible
          accessibilityLabel={accessibilityLabel}
          accessibilityHint={accessibilityHint}
          style={[styles.row, { backgroundColor: highlighted ? palette.selected : palette.surface }]}
        >
          {rowContent}
        </View>
      )}
      {trailing ? <View style={styles.trailing}>{trailing}</View> : null}
    </View>
  );
}

const styles = StyleSheet.create({
  outer: { flexDirection: "row", alignItems: "center" },
  row: {
    flex: 1,
    flexDirection: "row",
    alignItems: "center",
    position: "relative",
    minHeight: 44,
    paddingLeft: 20,
    paddingRight: spacing.lg,
    paddingVertical: 10,
    gap: spacing.md,
  },
  // 9pt, absolutely centred in the 20pt left gutter.
  dot: {
    position: "absolute",
    left: 6,
    top: "50%",
    marginTop: -4.5,
    width: 9,
    height: 9,
    borderRadius: 4.5,
  },
  textColumn: {
    flex: 1,
    borderBottomWidth: StyleSheet.hairlineWidth,
    paddingBottom: 10,
  },
  firstLine: { flexDirection: "row", alignItems: "center", gap: spacing.sm },
  title: { flex: 1, fontFamily: fonts.sans, fontSize: 16, lineHeight: 21 },
  titleUnread: { fontFamily: fonts.sansSemiBold },
  time: { fontFamily: fonts.sans, fontSize: 13, lineHeight: 18 },
  preview: { fontFamily: fonts.sans, fontSize: 16, lineHeight: 21 },
  detail: { fontFamily: fonts.sans, fontSize: 13, lineHeight: 18 },
  trailing: { justifyContent: "center", paddingRight: spacing.lg },
});
