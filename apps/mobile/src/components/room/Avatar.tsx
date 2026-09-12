import { StyleSheet, Text, View } from "react-native";

import { colors, fonts, radii } from "../../config/theme";

/**
 * One conversation avatar. Agents get the tinted monogram the roster used
 * (emoji when set); humans get their initials on the same tile. No photos:
 * the room member row carries no imageUrl.
 */
export function Avatar({
  name,
  kind,
  emoji,
  size = 28,
}: {
  name: string;
  kind: "human" | "agent" | "system";
  emoji?: string;
  size?: number;
}) {
  const initials =
    name
      .trim()
      .split(/\s+/)
      .slice(0, 2)
      .map((word) => word[0] ?? "")
      .join("")
      .toUpperCase() || "?";
  return (
    <View
      style={[
        styles.tile,
        { width: size, height: size, borderRadius: size > 32 ? radii.lg : radii.md },
      ]}
    >
      {emoji ? (
        <Text style={{ fontSize: size * 0.5 }}>{emoji}</Text>
      ) : (
        <Text
          style={[
            kind === "agent" ? styles.agent : styles.human,
            { fontSize: size * 0.45 },
          ]}
        >
          {kind === "agent" ? initials[0] : initials}
        </Text>
      )}
    </View>
  );
}

const styles = StyleSheet.create({
  tile: { backgroundColor: colors.forestTint, alignItems: "center", justifyContent: "center" },
  agent: { fontFamily: fonts.loraBoldItalic, color: colors.forest },
  human: { fontFamily: fonts.sansSemiBold, color: colors.forest },
});
