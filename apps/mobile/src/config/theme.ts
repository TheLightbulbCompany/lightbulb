/**
 * Design tokens for the mobile app (RN has no CSS variables; this is the
 * StyleSheet-era stand-in until D6 revisits NativeWind in Phase 2+).
 * Values come VERBATIM from the web design spec
 * (.superpowers/sdd/web-design-spec.md §0) and must stay in lockstep with
 * the web frontend's cream/forest design language. ALL color literals live
 * here — screens import tokens, never raw colors.
 */
import { Platform } from "react-native";
import type { TextStyle } from "react-native";

/** Spec-§0 palette. Names mirror the web `:root` custom properties. */
const palette = {
  // Surfaces
  /** Page / main background (`--cream`). */
  cream: "#faf7f2",
  /** Sidebar, sunken wells, code-header (`--cream-deeper`). */
  creamDeeper: "#f3efe6",
  /** Deepest sunken surface (`--cream-deepest`). */
  creamDeepest: "#ebe5d6",
  /** User chat bubble background. */
  userBubble: "#f0ebe2",
  /** Inline code / chip background. */
  chip: "#e8e3d9",
  /** Fenced code block body background. */
  codeBg: "#f8f5f0",
  /** Loading skeleton fill (§7). */
  skeleton: "#eee9df",
  white: "#ffffff",

  // Borders
  /** Hairline borders (`--edge`). */
  edge: "#e0dbd0",
  /** Softer hairline (`--edge-soft`). */
  edgeSoft: "#ebe6da",
  /** Forest-tinted hairline (`--edge-forest`). */
  edgeForest: "#d4e6dc",

  // Brand
  /** Primary CTA, links, focus (`--forest`). */
  forest: "#06402B",
  /** Soft brand green — active status dots (`--forest-soft`). */
  forestSoft: "#4a9e74",
  /** Brand tint surface — monogram bg (`--forest-tint`). */
  forestTint: "#e6efe9",
  /** Halo ring around active status dots (§5). */
  forestHalo: "rgba(74,158,116,.18)",
  /** Accent gold — published badges (`--gold`). */
  gold: "#c2a378",

  // Text
  /** Primary text (`--ink`). */
  ink: "#1a1a1a",
  /** Secondary text (`--ink-soft`). */
  inkSoft: "#3d3d3d",
  /** Assistant message body text. */
  assistantInk: "#302d28",
  /** Muted text (`--mute`). */
  mute: "#8a8578",
  /** Softest muted text / paused dots (`--mute-soft`). */
  muteSoft: "#b5af9f",
  /** Section-label counts (§5). */
  sectionCount: "#a8a395",
  /** Input placeholders. */
  placeholder: "#b5ae9e",

  // Status
  /** Warning / thinking (`--warn`). */
  warn: "#c47a1d",
  /** Destructive / error status dots (`--danger`). */
  danger: "#b32424",
  /** Success / connected (running-green). */
  runningGreen: "#2d8a4e",
  /** Connecting / disconnected-banner pulsing dot (§4). */
  connectingYellow: "#c9a227",

  // Error card (§4)
  errorBg: "#fce4ec",
  errorText: "#a5311f",
  errorBorder: "#f8bbd0",

  // Disconnected banner (§4)
  bannerBg: "#fdf6e3",
  bannerBorder: "#ede3c4",
  bannerText: "#8a7340",

  // Roster row overlays (§5)
  rowPressed: "rgba(255,255,255,.85)",
  rowActive: "rgba(255,255,255,.9)",

  // Tool-pill status trios (§3)
  tool: {
    running: {
      bg: "#e8f5e9",
      text: "#2d8a4e",
      border: "#c8e6c9",
      dot: "#2d8a4e",
    },
    done: { bg: "#f3efe6", text: "#8a8578", border: "#e0dbd0", dot: "#cdc7ba" },
    error: {
      bg: "#fce4ec",
      text: "#a5311f",
      border: "#f8bbd0",
      dot: "#c62828",
    },
  },
} as const;

export const colors = {
  ...palette,
  // Legacy aliases (pre-parity names still used by existing screens).
  /** @deprecated use `creamDeeper` */
  creamRaised: palette.creamDeeper,
  /** @deprecated use `chip` */
  creamHover: palette.chip,
  /** @deprecated use `edge` */
  border: palette.edge,
  /** @deprecated use `mute` */
  muted: palette.mute,
  /** @deprecated use `inkSoft` */
  mutedSecondary: palette.inkSoft,
  /** @deprecated use `runningGreen` */
  green: palette.runningGreen,
  /** @deprecated use `forestSoft` */
  greenLight: palette.forestSoft,
  /** @deprecated use `danger` */
  error: palette.danger,
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
  /** Decorative "8" glyph / empty-state serif (§6). */
  loraItalic: "Lora_400Regular_Italic",
  /** Agent monograms (§5). */
  loraBoldItalic: "Lora_700Bold_Italic",
  /** Platform monospace stand-in for Geist Mono (§0 RN notes). */
  mono: Platform.select({ ios: "Menlo", default: "monospace" }) as string,
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
  /** Mono section labels — 10px, tracked .14em, uppercase content (§5/§6). */
  overline: {
    fontFamily: fonts.mono,
    fontSize: 10,
    lineHeight: 14,
    letterSpacing: 1.4,
    color: colors.mute,
  },
} as const satisfies Record<string, TextStyle>;

export const spacing = {
  xs: 4,
  sm: 8,
  md: 12,
  lg: 16,
  xl: 24,
} as const;

/** Spec radius scale: 4 / 6 / 8 / 12 / 16 (§0). */
export const radii = {
  xs: 4,
  sm: 6,
  md: 8,
  lg: 12,
  xl: 16,
} as const;
