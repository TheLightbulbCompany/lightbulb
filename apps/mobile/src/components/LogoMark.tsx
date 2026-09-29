/**
 * The product bulb (Brand Identity Manual §01/§02) — the mark that replaced
 * the retired "8" glyph.
 *
 * Web draws it as inline SVG (`components/Logo.tsx`). Mobile has no
 * `react-native-svg` and the mark is not worth a native dependency, so this
 * renders the same artwork from `assets/images/mark.png` — its OWN asset, not
 * the app icon: pointing at the icon meant a change to the launcher artwork
 * silently changed every in-app mark, and the icon's 1024² canvas made RN
 * decode ~4 MB to draw a 15px mark.
 *
 * The artwork is cropped to the glass, so `size` is its HEIGHT and the width
 * follows the asset's own 356:793 ratio. The asset's background is
 * transparent (chroma-keyed from its original opaque cream export — 2026-09,
 * dark-mode pass), so the mark sits seamlessly on any ground, light or dark.
 */
import { Image } from "expo-image";
import type { ImageStyle, StyleProp } from "react-native";

const MARK = require("../../assets/images/mark.png");

/** The asset's intrinsic aspect (356 × 793) — the bulb fills it edge to edge. */
const ASPECT = 356 / 793;

export function LogoMark({
  size = 48,
  decorative = false,
  style,
}: {
  /** Height in px. Width follows the mark's own ratio. */
  size?: number;
  /**
   * Hide from the accessibility tree. Set it wherever the mark repeats as
   * ornament, so VoiceOver doesn't read "Lightbulb, image" as noise.
   */
  decorative?: boolean;
  style?: StyleProp<ImageStyle>;
}) {
  return (
    <Image
      source={MARK}
      style={[
        { width: Math.round(size * ASPECT), height: size, alignSelf: "center" },
        style,
      ]}
      contentFit="contain"
      accessible={!decorative}
      accessibilityElementsHidden={decorative}
      importantForAccessibility={decorative ? "no-hide-descendants" : "yes"}
      accessibilityRole={decorative ? undefined : "image"}
      accessibilityLabel={decorative ? undefined : "Lightbulb"}
    />
  );
}
