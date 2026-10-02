import type { ReactNode } from "react";
import { Pressable, StyleSheet, Text, View } from "react-native";

import { type ChatPalette, type } from "../../config/theme";

/**
 * A native Stack `headerTitle`: avatar + name + a one-line status ("working…",
 * "idle", "12 members", "Working on Leo's Mac"). Tapping it opens the
 * conversation's details, mirroring the pre-redesign room header.
 */
export function ConversationHeader(props: {
  title: string;
  /** What VoiceOver reads in place of `title`, when the avatar carries part of the name (a session's tool). */
  accessibilityTitle?: string;
  status?: string | null;
  /** `live` = `palette.live` (an agent working / a busy session); otherwise `palette.fgMute`. */
  statusTone?: "mute" | "live";
  /** 28pt Avatar (or the channel `#` tile). */
  avatar: ReactNode;
  palette: ChatPalette;
  /** Opens details. */
  onPress?: () => void;
  /** The native title view takes its content's width, so a long title needs this to truncate instead of running under the header's buttons. */
  maxWidth?: number;
}) {
  const { title, accessibilityTitle, status, statusTone = "mute", avatar, palette, onPress, maxWidth } = props;
  const spoken = accessibilityTitle ?? title;
  const label = status ? `${spoken}, ${status}` : spoken;
  const content = (
    <>
      <View style={styles.avatar}>{avatar}</View>
      <View style={styles.column}>
        <Text style={[type.navTitle, { color: palette.fg }]} numberOfLines={1} maxFontSizeMultiplier={1}>
          {title}
        </Text>
        {status ? (
          <Text
            style={[type.navMeta, styles.status, { color: statusTone === "live" ? palette.live : palette.fgMute }]}
            numberOfLines={1}
            maxFontSizeMultiplier={1}
          >
            {status}
          </Text>
        ) : null}
      </View>
    </>
  );
  // No `onPress`: a plain, inert container — no button role and no "Shows details" hint,
  // since there is nothing for VoiceOver to invoke (Codex review finding).
  if (!onPress) {
    return (
      <View style={[styles.row, { maxWidth }]} accessibilityLabel={label}>
        {content}
      </View>
    );
  }
  return (
    <Pressable
      style={[styles.row, { maxWidth }]}
      onPress={onPress}
      accessibilityRole="button"
      accessibilityLabel={label}
      accessibilityHint="Shows details"
    >
      {content}
    </Pressable>
  );
}

const styles = StyleSheet.create({
  row: {
    flexDirection: "row",
    alignItems: "center",
    gap: 4,
    flexShrink: 1,
  },
  // Room for the state ring, which draws 4pt outside the avatar's tile and was
  // clipped by the navigation bar's item bounds.
  avatar: {
    padding: 4,
  },
  column: {
    flexShrink: 1,
  },
  // The brief calls for `type.navMeta` at 12pt — `type.navMeta` itself is 11pt
  // (the two-line member-count case elsewhere), so this overrides the size.
  status: {
    fontSize: 12,
  },
});
