import { Pressable, StyleSheet, Text, View } from "react-native";

import { fonts, spacing, useChatPalette } from "../../config/theme";
import { TimelineSkeleton } from "../chat/TimelineSkeleton";

export type RoomBlockedStateKind = "loading" | "access-removed" | "unavailable";

/**
 * Copy verbatim from web's RoomPanel.tsx `room-blocked` guard (search
 * `room-blocked`) — the reference implementation for this state. Do not
 * paraphrase.
 */
const MESSAGE: Record<RoomBlockedStateKind, string> = {
  loading: "Loading chat…",
  "access-removed": "Access removed. You can no longer view this chat.",
  unavailable: "This chat is unavailable.",
};

/**
 * The mobile twin of web's `room-blocked` guard: replaces the WHOLE panel —
 * timeline and composer both — never a banner drawn over them. `accessRemoved`
 * means the server rotated the room's delivery key after removing this reader
 * (see the Messaging section of CLAUDE.md); the composer must go, not just
 * dim, so this has no children slot a caller could use to keep it mounted
 * underneath.
 */
export function RoomBlockedState({
  state,
  onRetry,
}: {
  state: RoomBlockedStateKind;
  onRetry?: () => void;
}) {
  const palette = useChatPalette();
  return (
    <View style={styles.wrap}>
      <Text style={[styles.message, { color: palette.fgMute }]}>{MESSAGE[state]}</Text>
      {state === "loading" && (
        <View style={styles.skeleton}>
          <TimelineSkeleton />
        </View>
      )}
      {state === "unavailable" && onRetry && (
        <Pressable onPress={onRetry} accessibilityRole="button" accessibilityLabel="Try again">
          <Text style={[styles.retryText, { color: palette.link }]}>Try again</Text>
        </Pressable>
      )}
    </View>
  );
}

const styles = StyleSheet.create({
  wrap: {
    flex: 1,
    alignItems: "center",
    justifyContent: "center",
    gap: spacing.lg,
    padding: spacing.xl,
  },
  message: {
    fontFamily: fonts.sans,
    fontSize: 15,
    lineHeight: 22,
    textAlign: "center",
  },
  skeleton: { alignSelf: "stretch" },
  retryText: { fontFamily: fonts.sansMedium, fontSize: 14, lineHeight: 18 },
});
