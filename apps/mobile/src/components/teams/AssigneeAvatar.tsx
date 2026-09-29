import { Image } from "expo-image";
import { StyleSheet, Text, useColorScheme, View } from "react-native";

import { chatPalette, fonts, radii, type ChatPalette } from "../../config/theme";
import type { AssigneeDisplay } from "../../lib/teamsData";

// ---------------------------------------------------------------------------
// Assignee avatar (Teams v3 T3a). Agents render a single-letter monogram on
// the agent-tint fill; human members render up to two initials on the
// selected fill, or their Clerk photo when one is present. Rounded squares,
// not circles (spec: avatars are rounded squares like web) — the photo is
// drawn OVER the initials with expo-image, so a failed/slow image load
// simply falls back to the initials underneath — no error state needed.
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

/** Rounded-square radius: 24pt+ reads as a real tile, under that a corner
 * that size would look almost circular, so it steps down to a small chip. */
function radiusFor(size: number): number {
  return size > 32 ? radii.lg : size >= 24 ? radii.md : radii.xs;
}

function makeStyles(p: ChatPalette) {
  return StyleSheet.create({
    tile: {
      alignItems: "center",
      justifyContent: "center",
      overflow: "hidden",
    },
    agentTile: { backgroundColor: p.agentTint },
    userTile: { backgroundColor: p.selected },
    glyph: { textAlign: "center" },
    agentGlyph: { fontFamily: fonts.sansSemiBold, color: p.onAgentTint },
    userGlyph: { fontFamily: fonts.sansSemiBold, color: p.fg },
    photo: { position: "absolute", top: 0, left: 0 },
  });
}

const SHEETS = { light: makeStyles(chatPalette.light), dark: makeStyles(chatPalette.dark) };

export interface AssigneeAvatarProps {
  assignee: AssigneeDisplay;
  /** Tile dimension in px (default 18, the board-card footer size). */
  size?: number;
}

export function AssigneeAvatar({ assignee, size = 18 }: AssigneeAvatarProps) {
  const styles = SHEETS[useColorScheme() === "dark" ? "dark" : "light"];
  const { name, kind, imageUrl } = assignee;
  const isAgent = kind === "agent";
  const glyph = initialsFor(name, kind);
  const dimension = { width: size, height: size, borderRadius: radiusFor(size) };
  // Scale the glyph to the tile; the board-card default (18px) keeps ~10px.
  const fontSize = Math.round(size * 0.55);

  return (
    <View
      testID="assignee-avatar"
      style={[
        styles.tile,
        dimension,
        isAgent ? styles.agentTile : styles.userTile,
      ]}
    >
      <Text
        maxFontSizeMultiplier={1}
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
