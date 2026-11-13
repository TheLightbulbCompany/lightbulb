import { StyleSheet, Text, View } from "react-native";

import { chatPalette, fonts, radii, type ChatPalette } from "../../config/theme";

/** One face on the Live sheet: agents get the tinted monogram, humans their initials. No photos. */
export function Avatar({ name, kind, size = 28, palette }: {
  name: string; kind: "human" | "agent"; size?: number; palette?: ChatPalette;
}) {
  const p = palette ?? chatPalette.light;
  const initials = name.trim().split(/\s+/).slice(0, 2).map((word) => word[0] ?? "").join("").toUpperCase() || "?";
  return (
    <View testID="avatar-tile" style={[styles.tile, {
      width: size, height: size, borderRadius: size > 32 ? radii.lg : radii.md,
      backgroundColor: kind === "agent" ? p.agentTint : p.selected,
    }]}>
      {/* Sized to the fixed tile, so it must not also scale with the reader's text size. */}
      <Text maxFontSizeMultiplier={1} style={[kind === "agent"
        ? { fontFamily: fonts.loraBoldItalic, color: p.onAgentTint }
        : { fontFamily: fonts.sansSemiBold, color: p.fg }, { fontSize: size * 0.45 }]}>
        {kind === "agent" ? initials[0] : initials}
      </Text>
    </View>
  );
}

const styles = StyleSheet.create({ tile: { alignItems: "center", justifyContent: "center" } });
