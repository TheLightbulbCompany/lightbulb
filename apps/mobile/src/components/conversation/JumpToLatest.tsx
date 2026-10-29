import { SymbolView } from "../ui/SymbolView";
import { StyleSheet, Text, View } from "react-native";

import { fonts, spacing, type ChatPalette } from "../../config/theme";
import { AnimatedPressable, usePressScale } from "../../lib/pressScale";

/**
 * The way back to the newest message, shown once the reader has scrolled away
 * from it; `count` is what arrived while they were away.
 *
 * A raised surface with an edge, not the ink fill the send button has: in dark
 * mode the reader's own bubbles are the same near-white, and the button
 * disappeared over them. It sits at the bottom of the timeline it belongs to,
 * so it stays clear of the composer whatever height that has grown to.
 */
export function JumpToLatest({ palette, onPress, count = 0 }: {
  palette: ChatPalette;
  onPress: () => void;
  count?: number;
}) {
  const scale = usePressScale();
  const label = count > 0 ? `Jump to latest, ${count} new ${count === 1 ? "message" : "messages"}` : "Jump to latest";
  return (
    <AnimatedPressable
      style={[styles.jump, { backgroundColor: palette.raised, borderColor: palette.edge }, scale.style]}
      onPress={onPress}
      onPressIn={scale.onPressIn}
      onPressOut={scale.onPressOut}
      hitSlop={6}
      accessibilityRole="button"
      accessibilityLabel={label}
    >
      <SymbolView accessibilityElementsHidden name="arrow.down" size={16} tintColor={palette.fg} />
      {count > 0 ? (
        <View style={[styles.count, { backgroundColor: palette.link }]}>
          <Text style={[styles.countText, { color: palette.onLink }]} maxFontSizeMultiplier={1.2}>
            {count > 99 ? "99+" : count}
          </Text>
        </View>
      ) : null}
    </AnimatedPressable>
  );
}

const styles = StyleSheet.create({
  jump: {
    position: "absolute",
    right: spacing.lg,
    bottom: spacing.md,
    width: 40,
    height: 40,
    borderRadius: 20,
    borderWidth: StyleSheet.hairlineWidth,
    alignItems: "center",
    justifyContent: "center",
  },
  count: {
    position: "absolute",
    top: -6,
    right: -4,
    minWidth: 20,
    height: 20,
    borderRadius: 10,
    paddingHorizontal: 6,
    alignItems: "center",
    justifyContent: "center",
  },
  countText: { fontFamily: fonts.sansSemiBold, fontSize: 12, lineHeight: 16 },
});
