import { Image, StyleSheet, Text, View } from "react-native";

import { colors, fonts } from "../../config/theme";
import type { AssigneeDisplay } from "../../lib/teamsData";

// ---------------------------------------------------------------------------
// Assignee avatar (Teams v3 T3a). Agents render a single-letter forest-tint
// monogram (matching the pre-v3 board card); human members render up to two
// initials in a neutral avatar, or their Clerk photo when one is present. The
// photo is drawn OVER the initials with the built-in RN Image (no native/remote
// image dep), so a failed/slow image load simply falls back to the initials
// underneath — no error state needed.
// ---------------------------------------------------------------------------

/** Single uppercase initial for agents; first+last initials for people. */
function initialsFor(name: string, kind: "agent" | "user"): string {
  const parts = name.trim().split(/\s+/).filter(Boolean);
  if (parts.length === 0) return "?";
  if (kind === "agent" || parts.length === 1) {
    return parts[0].charAt(0).toUpperCase();
  }
  return (parts[0].charAt(0) + parts[parts.length - 1].charAt(0)).toUpperCase();
}

export interface AssigneeAvatarProps {
  assignee: AssigneeDisplay;
  /** Circle diameter in px (default 18, the board-card footer size). */
  size?: number;
}

export function AssigneeAvatar({ assignee, size = 18 }: AssigneeAvatarProps) {
  const { name, kind, imageUrl } = assignee;
  const isAgent = kind === "agent";
  const glyph = initialsFor(name, kind);
  const dimension = { width: size, height: size, borderRadius: size / 2 };
  // Scale the glyph to the circle; the board-card default (18px) keeps ~10px.
  const fontSize = Math.round(size * 0.55);

  return (
    <View
      style={[
        styles.circle,
        dimension,
        isAgent ? styles.agentCircle : styles.userCircle,
      ]}
    >
      <Text
        style={[
          styles.glyph,
          { fontSize, lineHeight: fontSize + 2 },
          isAgent ? styles.agentGlyph : styles.userGlyph,
        ]}
        numberOfLines={1}
      >
        {glyph}
      </Text>
      {!isAgent && imageUrl ? (
        <Image
          source={{ uri: imageUrl }}
          style={[styles.photo, dimension]}
          accessibilityIgnoresInvertColors
        />
      ) : null}
    </View>
  );
}

const styles = StyleSheet.create({
  circle: {
    alignItems: "center",
    justifyContent: "center",
    overflow: "hidden",
  },
  agentCircle: { backgroundColor: colors.forestTint },
  userCircle: { backgroundColor: colors.avatarBg },
  glyph: { textAlign: "center" },
  agentGlyph: { fontFamily: fonts.sansSemiBold, color: colors.forest },
  userGlyph: { fontFamily: fonts.sansSemiBold, color: colors.avatarText },
  photo: { position: "absolute", top: 0, left: 0 },
});
