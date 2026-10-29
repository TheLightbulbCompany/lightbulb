import type { AvatarRing } from "@lightbulb/shared";
import { SymbolView } from "../ui/SymbolView";
import { StyleSheet, Text, View } from "react-native";

import { chatPalette, fonts, radii, type ChatPalette } from "../../config/theme";
import { SessionToolIcon } from "../session/SessionToolIcon";

export const RING_LABEL: Record<AvatarRing, string> = { running: "Working", attention: "Needs attention", paused: "Paused" };
const RING_COLOR: Record<AvatarRing, keyof ChatPalette> = { running: "live", attention: "alert", paused: "warn" };

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
  ring = null,
  vendor,
  palette,
}: {
  name: string;
  kind: "human" | "agent" | "system";
  emoji?: string;
  size?: number;
  /**
   * Presence for a human (a recent heartbeat) -- wired via `usePresence` on
   * mobile. Ignored for agents, which are always shown as reachable
   * regardless of this prop.
   */
  online?: boolean;
  /** Off by default (stacked facepiles, message rows, the composer); turn on
   *  for a single DM face, mirroring web's ParticipantAvatar `showPresence` gate. */
  showPresence?: boolean;
  /** The human owns a live shared terminal session right now. Ignored for agents. */
  sharing?: boolean;
  /** State ring, drawn 2pt outside the tile. `null`/omitted draws nothing. */
  ring?: AvatarRing | null;
  /** A coding tool ("claude" | "codex" | other) -- draws its mark instead of initials. */
  vendor?: string;
  /** Defaults to the light chat palette. */
  palette?: ChatPalette;
}) {
  const p = palette ?? chatPalette.light;
  const radius = size > 32 ? radii.lg : radii.md;
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
  const dotColor = isAgent ? p.link : isOnlineHuman ? p.live : undefined;
  const dotLabel = isAgent ? "Agent, always available" : "Online";
  return (
    <View style={{ width: size, height: size }}>
      {ring ? (
        <View
          accessible
          accessibilityLabel={RING_LABEL[ring]}
          style={{
            position: "absolute",
            top: -4,
            left: -4,
            right: -4,
            bottom: -4,
            borderRadius: radius + 4,
            borderWidth: 2,
            borderColor: p[RING_COLOR[ring]],
          }}
        />
      ) : null}
      <View
        testID="avatar-tile"
        style={[
          styles.tile,
          {
            width: size,
            height: size,
            borderRadius: radius,
            backgroundColor: vendor ? p.surface : kind === "agent" ? p.agentTint : p.selected,
          },
          vendor ? { borderWidth: StyleSheet.hairlineWidth, borderColor: p.edge } : null,
        ]}
      >
        {vendor ? (
          <SessionToolIcon provider={vendor} size={Math.round(size * 0.55)} tint={p.fg} />
        ) : emoji ? (
          <Text style={{ fontSize: size * 0.5 }} maxFontSizeMultiplier={1}>
            {emoji}
          </Text>
        ) : (
          // Sized to the fixed tile, so it must not also scale with the
          // reader's text size or it spills out of it.
          <Text
            style={[
              kind === "agent"
                ? { fontFamily: fonts.loraBoldItalic, color: p.onAgentTint }
                : { fontFamily: fonts.sansSemiBold, color: p.fg },
              { fontSize: size * 0.45 },
            ]}
            maxFontSizeMultiplier={1}
          >
            {kind === "agent" ? initials[0] : initials}
          </Text>
        )}
      </View>
      {dotColor && (
        <View
          style={[styles.dot, { backgroundColor: dotColor, borderColor: p.surface }]}
          accessible
          accessibilityLabel={dotLabel}
        />
      )}
      {showPresence && sharing && kind === "human" && (
        <View
          style={[
            styles.sharing,
            {
              width: Math.max(10, size * 0.42),
              height: Math.max(10, size * 0.42),
              backgroundColor: p.fg,
              borderColor: p.surface,
            },
          ]}
          accessibilityLabel="Sharing a terminal session"
        >
          <SymbolView accessibilityElementsHidden name="terminal" size={Math.max(7, size * 0.28)} tintColor={p.onFg} />
        </View>
      )}
    </View>
  );
}

const styles = StyleSheet.create({
  tile: { alignItems: "center", justifyContent: "center" },
  // ~8pt dot, 2pt ring in the surface it sits on, bottom-right -- same as web.
  dot: {
    position: "absolute",
    right: -1,
    bottom: -1,
    width: 8,
    height: 8,
    borderRadius: 4,
    borderWidth: 2,
  },
  // Opposite corner from the presence dot -- a sharer can also be online.
  sharing: {
    position: "absolute",
    left: -2,
    bottom: -2,
    borderRadius: 3,
    alignItems: "center",
    justifyContent: "center",
    borderWidth: 1.5,
  },
});
