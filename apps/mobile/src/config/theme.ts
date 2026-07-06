/**
 * Design tokens for the mobile app (RN has no CSS variables; this is the
 * StyleSheet-era stand-in until D6 revisits NativeWind in Phase 2+).
 * Values are extracted from the web frontend's cream/forest design language
 * and must stay in lockstep with it. ALL hex literals live here — screens
 * import tokens, never raw colors.
 */
import type { TextStyle } from "react-native";

export const colors = {
  /** Primary accent / CTA. */
  forest: "#06402B",
  /** Success / highlight. */
  green: "#2d8a4e",
  /** Light variant of green. */
  greenLight: "#4a9e74",
  /** Page background. */
  cream: "#faf7f2",
  /** Raised / secondary surface. */
  creamRaised: "#f3efe6",
  /** Tertiary surface. */
  creamTertiary: "#f0ebe0",
  /** Hover / pressed surface. */
  creamHover: "#e8e3d9",
  /** Hairline borders. */
  border: "#e0dbd0",
  /** Primary text. */
  ink: "#1a1a1a",
  /** Muted text. */
  muted: "#8a8578",
  /** Secondary muted text. */
  mutedSecondary: "#5c5c5c",
  /** Destructive / error. */
  error: "#dc2626",
  white: "#ffffff",
} as const;

/**
 * Post-load font family names (what `useFonts` in the root layout registers).
 * Pass these straight to `fontFamily`. If the font download fails, RN falls
 * back to the system font for unknown family names — never a crash.
 */
export const fonts = {
  dmSans: "DMSans_400Regular",
  dmSansMedium: "DMSans_500Medium",
  dmSansBold: "DMSans_700Bold",
  lora: "Lora_400Regular",
  loraBold: "Lora_700Bold",
} as const;

/** Reusable text styles mirroring the web type scale. */
export const type = {
  display: {
    fontFamily: fonts.lora,
    fontSize: 32,
    lineHeight: 38,
    color: colors.ink,
  },
  title: {
    fontFamily: fonts.dmSansBold,
    fontSize: 20,
    lineHeight: 26,
    color: colors.ink,
  },
  body: {
    fontFamily: fonts.dmSans,
    fontSize: 15,
    lineHeight: 22,
    color: colors.ink,
  },
  caption: {
    fontFamily: fonts.dmSans,
    fontSize: 12,
    lineHeight: 16,
    color: colors.muted,
  },
} as const satisfies Record<string, TextStyle>;

export const spacing = {
  xs: 4,
  sm: 8,
  md: 12,
  lg: 16,
  xl: 24,
} as const;

export const radii = {
  sm: 8,
  md: 14,
  lg: 18,
} as const;
