/**
 * The product bulb (Brand Identity Manual §01/§02) — the mark that replaced
 * the retired "8" glyph.
 *
 * Web draws it as inline SVG (`components/Logo.tsx`). Mobile has no
 * `react-native-svg` and the mark is not worth a native dependency, so this
 * renders the same artwork from the app icon already in the bundle. That PNG
 * is drawn on the manual's cream (#F5EFE2, verified against the file), so it
 * sits seamlessly on any `colors.cream` ground — which is where every mark
 * site is. On a deeper surface it would show its square; use it on cream.
 */
import { Image, StyleSheet } from "react-native";
import type { ImageStyle, StyleProp } from "react-native";

const MARK = require("../../assets/images/icon.png");

export function LogoMark({
  size = 48,
  style,
}: {
  size?: number;
  style?: StyleProp<ImageStyle>;
}) {
  return (
    <Image
      source={MARK}
      style={[styles.mark, { width: size, height: size }, style]}
      resizeMode="contain"
      accessible
      accessibilityRole="image"
      accessibilityLabel="Lightbulb"
    />
  );
}

const styles = StyleSheet.create({
  // The glass is taller than it is wide, so the artwork is padded; contain
  // keeps it centred at every size.
  mark: { alignSelf: "center" },
});
