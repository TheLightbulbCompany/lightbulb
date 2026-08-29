/**
 * Design tokens for the mobile app (RN has no CSS variables; this is the
 * StyleSheet-era stand-in).
 *
 * Values come from the Lightbulb Brand Identity Manual and mirror the web
 * frontend's `globals.css` `:root` block one-for-one. Four colours only —
 * midnight #0F1A2B (dark ground, ALL body copy on cream), cream #F5EFE2
 * (light ground, never pure white), bulb #F0C34E (the light — never type,
 * never a fill), brass #B68235 (rules and small labels ON MIDNIGHT only;
 * 2.94:1 on cream, so never type there). ALL color literals live here —
 * screens import tokens, never raw colors.
 *
 * The text steps are midnight mixed into cream, i.e. the literal value of
 * web's `color-mix(in srgb, #0F1A2B N%, #F5EFE2)`. Each carries its mix and
 * its measured contrast on cream so the next person can re-derive it.
 *
 * The app is cream-only — there is no midnight surface — so the reversed
 * halves of the state pairs (#7FB58D / #E09A55 / #DB8C7C / #93B4C9) are
 * deliberately absent. Add them when a midnight surface ships.
 *
 * State rules (§06): a state colour marks text, an icon, a border or a thin
 * rule — never a large fill. A ≤10% alpha wash behind a chip or banner is the
 * one exception, which is what the `rgba(…,.10)` tints below are.
 */
import { Platform } from "react-native";
import type { TextStyle } from "react-native";

/** The manual's palette. Key names predate the rebrand; the values are current. */
const palette = {
  // Surfaces
  /** Page / main background (`--cream`). Never pure white. */
  cream: "#F5EFE2",
  /** The one surface step — hover/selected, inset wells (`--cream-deep`). */
  creamDeeper: "#EFE8D9",
  /** The second step — pressed, table stripe (`--cream-deeper`). */
  creamDeepest: "#E7DECB",
  /** User chat bubble background — the one surface step. */
  userBubble: "#EFE8D9",
  /** Inline code / chip background — the one surface step. */
  chip: "#EFE8D9",
  /** Pressed sunken header / disabled send-button fill — the second step. */
  creamPressed: "#E7DECB",
  /** Fenced code block body background — the one surface step. */
  codeBg: "#EFE8D9",
  /** Loading skeleton fill — the one surface step. */
  skeleton: "#EFE8D9",
  /**
   * NOT white. The manual has no white; cream is the lightest ground there
   * is, and cards are delineated by an `edge` border, not by a surface shift
   * (same contract as web's `--card: var(--cream)`). Kept as a key because
   * ~98 call sites read it, including button labels on the midnight CTA,
   * where cream-on-midnight is exactly right.
   */
  white: "#F5EFE2",

  // Borders
  /** Hairline borders on cream (`--edge`). */
  edge: "#DED5C2",
  /** Softer separation on cream (`--edge-soft`). */
  edgeSoft: "#E8E0D0",
  /** Was a forest-tinted hairline; the manual has one hairline (`--edge`). */
  edgeForest: "#DED5C2",

  // Brand
  /** Midnight — primary CTA, links, focus (`--midnight`). */
  forest: "#0F1A2B",
  /** Success — active status dots (`--ok`). 5.44:1 on cream. */
  forestSoft: "#3A6B45",
  /** Brand tint surface — monogram bg. The one surface step. */
  forestTint: "#EFE8D9",
  /** Composer focus ring — midnight at 25%. */
  forestFocusBorder: "rgba(15,26,43,0.25)",
  /** Halo ring around active status dots — success at 18%. */
  forestHalo: "rgba(58,107,69,0.18)",
  /**
   * Brass (`--brass`) — rules and small marks ONLY. It measures 2.94:1 on
   * cream, so it is never type here. It currently has no call sites; if one
   * appears that renders brass TEXT on cream, that site wants `ink`.
   */
  gold: "#B68235",
  /** Info (`--info`) — in_review status. 6.76:1 on cream. */
  info: "#35566F",

  // Text — midnight mixed into cream, contrast measured against cream.
  /** Primary text (`--midnight`). 15.24:1. */
  ink: "#0F1A2B",
  /** Secondary text — midnight 80% into cream. 8.46:1. */
  inkSoft: "#3D4550",
  /** Assistant message body text — primary ink. */
  assistantInk: "#0F1A2B",
  /** Tertiary / metadata text — midnight 64% into cream. 4.98:1. */
  mute: "#62676D",
  /**
   * Disabled text and non-text marks (dots, chevrons, rules) — midnight 50%
   * into cream. 3.28:1, which clears the 3:1 non-text bar but NOT the 4.5:1
   * text floor. Anything that reads as text belongs on `mute`.
   */
  muteSoft: "#828486",
  /** Small uppercase labels — midnight 61% into cream. 4.55:1, the lowest step over AA. */
  sectionCount: "#696D72",
  /** Input placeholders — midnight 64% into cream. 4.98:1. */
  placeholder: "#62676D",

  // Status — the manual's state pairs, on-cream halves (§06).
  /** Warning / thinking (`--warn`). 5.30:1. */
  warn: "#96501A",
  /** Destructive / error (`--err`). 6.33:1. */
  danger: "#96382B",
  /** Success / connected (`--ok`). 5.44:1. */
  runningGreen: "#3A6B45",
  /** Connecting / disconnected-banner pulsing dot — warning. */
  connectingYellow: "#96501A",

  // Error card — a 10% wash behind error type and an error rule, never a fill.
  errorBg: "rgba(150,56,43,0.10)",
  errorText: "#96382B",
  errorBorder: "#96382B",

  // Disconnected banner — the surface step, ruled and marked in warning.
  bannerBg: "#EFE8D9",
  bannerBorder: "#96501A",
  bannerText: "#96501A",

  // Footer user card — initials avatar
  /** Initials-avatar fill — the one surface step. */
  avatarBg: "#EFE8D9",
  /** Initials-avatar text — primary ink. */
  avatarText: "#0F1A2B",

  // Roster row overlays
  rowPressed: "rgba(245,239,226,.85)",
  rowActive: "rgba(245,239,226,.9)",
  /** Active session row background — midnight at 8%. */
  sessionActive: "rgba(15,26,43,.08)",
  /** Modal backdrop scrim behind bottom sheets — midnight at 35%. */
  scrim: "rgba(15,26,43,.35)",

  // Board status-chip tints (Teams board cards) — a ≤10% wash of the chip's
  // own state colour, the one fill the manual allows a state colour. Neutral
  // statuses get the surface step instead, because mute is not a state.
  statusTintBacklog: "#EFE8D9",
  statusTintTodo: "rgba(150,80,26,.10)",
  statusTintProgress: "rgba(150,80,26,.10)",
  statusTintReview: "rgba(53,86,111,.10)",
  statusTintBlocked: "rgba(150,56,43,.10)",
  statusTintCancelled: "#EFE8D9",
  statusTintDone: "rgba(58,107,69,.10)",

  // Tool-pill status trios — wash + state-coloured type, dot and border.
  tool: {
    running: {
      bg: "rgba(58,107,69,.10)",
      text: "#3A6B45",
      border: "#3A6B45",
      dot: "#3A6B45",
    },
    done: { bg: "#EFE8D9", text: "#62676D", border: "#DED5C2", dot: "#828486" },
    error: {
      bg: "rgba(150,56,43,.10)",
      text: "#96382B",
      border: "#96382B",
      dot: "#96382B",
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

  // ---------------------------------------------------------------------------
  // Teams board — status + priority semantic tokens (Teams v2, contract §3).
  // The board card must make status/priority instantly scannable, so every
  // status maps to a chip `color` (accent + chip text) and a `tint` (chip bg).
  // Mapped onto the manual's state pairs: backlog/cancelled=mute (not a state,
  // so a neutral surface step) · todo/in_progress=warn · in_review=info ·
  // blocked=err · done=ok. Never colour alone — the chip carries its label.
  // ---------------------------------------------------------------------------
  status: {
    backlog: { color: palette.mute, tint: palette.statusTintBacklog },
    todo: { color: palette.warn, tint: palette.statusTintTodo },
    in_progress: { color: palette.warn, tint: palette.statusTintProgress },
    in_review: { color: palette.info, tint: palette.statusTintReview },
    blocked: { color: palette.danger, tint: palette.statusTintBlocked },
    done: { color: palette.runningGreen, tint: palette.statusTintDone },
    cancelled: { color: palette.mute, tint: palette.statusTintCancelled },
  },
  /** Priority accent colors (contract §3): critical/high=err, medium=warn, low=mute. */
  priority: {
    critical: palette.danger,
    high: palette.danger,
    medium: palette.warn,
    low: palette.mute,
  },
  /** Fallback pill border/text for a label with no server-supplied color — it is text, so `mute`. */
  labelPillFallback: palette.mute,
} as const;

/**
 * Post-load font family names (what `useFonts` in the root layout registers).
 * Pass these straight to `fontFamily`. If the font download fails, RN falls
 * back to the system font for unknown family names — never a crash.
 *
 * §03: Source Sans 3 for everything that is not prose (labels, buttons, tabs,
 * inputs; 400/500/600, never below 400), Lora for prose and display, platform
 * mono for code only.
 */
export const fonts = {
  sans: "SourceSans3_400Regular",
  sansMedium: "SourceSans3_500Medium",
  sansSemiBold: "SourceSans3_600SemiBold",
  lora: "Lora_400Regular",
  /** Display headings and the wordmark (§03: Lora Medium, never 700). */
  loraMedium: "Lora_500Medium",
  loraBold: "Lora_700Bold",
  /** Prose italic — taglines, agent vibe lines. */
  loraItalic: "Lora_400Regular_Italic",
  /** Agent monograms. */
  loraBoldItalic: "Lora_700Bold_Italic",
  /** Platform monospace — code only. */
  mono: Platform.select({ ios: "Menlo", default: "monospace" }) as string,
} as const;

/** Reusable text styles mirroring the web type scale. */
export const type = {
  display: {
    fontFamily: fonts.loraMedium,
    fontSize: 32,
    lineHeight: 38,
    color: colors.ink,
  },
  title: {
    fontFamily: fonts.sansSemiBold,
    fontSize: 20,
    lineHeight: 26,
    color: colors.ink,
  },
  body: {
    fontFamily: fonts.sans,
    fontSize: 15,
    lineHeight: 22,
    color: colors.ink,
  },
  caption: {
    fontFamily: fonts.sans,
    fontSize: 12,
    lineHeight: 16,
    color: colors.muted,
  },
  /** Small uppercase labels — Source Sans 3 500, 10px, tracked 0.19em (§03/§06). */
  overline: {
    fontFamily: fonts.sansMedium,
    fontSize: 10,
    lineHeight: 14,
    letterSpacing: 1.9,
    color: colors.sectionCount,
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
