import { SymbolView } from "expo-symbols";
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
  online,
  showPresence = false,
  sharing,
}: {
  name: string;
  kind: "human" | "agent" | "system";
  emoji?: string;
  size?: number;
  /**
   * Presence for a human (a recent heartbeat). Not yet wired on mobile --
   * RoomMember carries no `online` field here, so this stays undefined until
   * that data reaches the app. Ignored for agents, which are always shown as
   * reachable regardless of this prop.
   */
  online?: boolean;
  /** Off by default (stacked facepiles, message rows, the composer); turn on
   *  for a single DM face, mirroring web's ParticipantAvatar `showPresence` gate. */
  showPresence?: boolean;
  /** The human owns a live shared terminal session right now. Ignored for agents. */
  sharing?: boolean;
}) {
  const initials =
    name
      .trim()
      .split(/\s+/)
      .slice(0, 2)
      .map((word) => word[0] ?? "")
      .join("")
      .toUpperCase() || "?";
  const isAgent = showPresence && kind === "agent";
  const isOnlineHuman = showPresence && kind === "human" && online === true;
  const dotColor = isAgent ? colors.presenceAgent : isOnlineHuman ? colors.presenceOnline : undefined;
  const dotLabel = isAgent ? "Agent, always available" : "Online";
  return (
    <View style={{ width: size, height: size }}>
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
      {dotColor && (
        <View
          style={[styles.dot, { backgroundColor: dotColor }]}
          accessible
          accessibilityLabel={dotLabel}
        />
      )}
      {showPresence && sharing && kind === "human" && (
        <View
          style={[styles.sharing, { width: Math.max(10, size * 0.42), height: Math.max(10, size * 0.42) }]}
          accessibilityLabel="Sharing a terminal session"
        >
          <SymbolView name="terminal" size={Math.max(7, size * 0.28)} tintColor={colors.white} />
        </View>
      )}
    </View>
  );
}

const styles = StyleSheet.create({
  tile: { backgroundColor: colors.forestTint, alignItems: "center", justifyContent: "center" },
  agent: { fontFamily: fonts.loraBoldItalic, color: colors.forest },
  human: { fontFamily: fonts.sansSemiBold, color: colors.forest },
  // ~8pt dot, 2pt ring in the surface it sits on, bottom-right -- same as web.
  dot: {
    position: "absolute",
    right: -1,
    bottom: -1,
    width: 8,
    height: 8,
    borderRadius: 4,
    borderWidth: 2,
    borderColor: colors.cream,
  },
  // Opposite corner from the presence dot -- a sharer can also be online.
  sharing: {
    position: "absolute",
    left: -2,
    bottom: -2,
    borderRadius: 3,
    backgroundColor: colors.forest,
    alignItems: "center",
    justifyContent: "center",
    borderWidth: 1.5,
    borderColor: colors.cream,
  },
});
