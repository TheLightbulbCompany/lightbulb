/**
 * 2026-09-12: the web moved to Sphere's quiet chrome (#2547 / #2536) — white
 * ground and neutral greys instead of warm cream and navy — and these values
 * now mirror main's `globals.css` light block one-for-one (cream #ffffff,
 * cream-deep #fafafa, cream-deeper #ededed, edge #e4e4e7, midnight #242428,
 * midnight-soft #45454b, midnight-muted #6b6b73, midnight-faint #8e8e94,
 * info #1e52cc). The state colours (ok/warn/err) and the brand marks (bulb,
 * brass) did not change. The web's `.dark` block has no mobile counterpart
 * yet — dark mode is Phase B, keyed off useColorScheme.
 *
 * Design tokens for the mobile app (RN has no CSS variables; this is the
 * StyleSheet-era stand-in).
 *
 * Values come from the Lightbulb Brand Identity Manual and mirror the web
 * frontend's `globals.css` `:root` block one-for-one. Four colours only —
 * midnight #242428 (dark ground, ALL body copy on cream), cream #ffffff
 * (light ground, never pure white), bulb #F0C34E (the light — never type,
 * never a fill), brass #B68235 (rules and small labels ON MIDNIGHT only;
 * 2.94:1 on cream, so never type there). ALL color literals live here —
 * screens import tokens, never raw colors.
 *
 * The text steps are midnight mixed into cream, i.e. the literal value of
 * web's `color-mix(in srgb, #242428 N%, #ffffff)`. Each carries its mix and
 * its measured contrast on cream so the next person can re-derive it.
 *
 * The app is cream-only — there is no midnight surface — so the reversed
 * halves of the state pairs (#7FB58D / #E09A55 / #DB8C7C / #93B4C9) are
 * deliberately absent. Add them when a midnight surface ships.
 *
 * State rules (§06): a state colour marks text, an icon, a border or a thin
 * rule — never a large fill. Two exceptions: a ≤10% alpha wash behind a chip
 * or banner (what the `rgba(…,.10)` tints below are), and THE destructive
 * confirm, which may be solid `danger` the way web's `--destructive` button
 * is — one per sheet, never a decorative fill.
 */
import { Platform } from "react-native";
import type { TextStyle } from "react-native";

/** The manual's palette. Key names predate the rebrand; the values are current. */
const palette = {
  // Surfaces
  /** Page / main background (`--cream`). Never pure white. */
  cream: "#ffffff",
  /** The one surface step — hover/selected, inset wells (`--cream-deep`). */
  creamDeeper: "#fafafa",
  /** The second step — pressed, table stripe (`--cream-deeper`). */
  creamDeepest: "#ededed",
  /** User chat bubble background — the one surface step. */
  userBubble: "#fafafa",
  /** Inline code / chip background — the one surface step. */
  chip: "#fafafa",
  /** Pressed sunken header / disabled send-button fill — the second step. */
  creamPressed: "#ededed",
  /** Fenced code block body background — the one surface step. */
  codeBg: "#fafafa",
  /** Loading skeleton fill — the one surface step. */
  skeleton: "#fafafa",
  /**
   * NOT white. The manual has no white; cream is the lightest ground there
   * is, and cards are delineated by an `edge` border, not by a surface shift
   * (same contract as web's `--card: var(--cream)`). Kept as a key because
   * ~98 call sites read it, including button labels on the midnight CTA,
   * where cream-on-midnight is exactly right.
   */
  white: "#ffffff",

  // Borders
  /** Hairline borders on cream (`--edge`). */
  edge: "#e4e4e7",
  /** Softer separation on cream (`--edge-soft`). */
  edgeSoft: "#f0f0f2",
  /** Was a forest-tinted hairline; the manual has one hairline (`--edge`). */
  edgeForest: "#e4e4e7",

  // Brand
  /** Midnight — primary CTA, links, focus (`--midnight`). */
  forest: "#242428",
  /** Success — active status dots (`--ok`). 5.44:1 on cream. */
  forestSoft: "#3A6B45",
  /** Brand tint surface — monogram bg. The one surface step. */
  forestTint: "#fafafa",
  /** Composer focus ring — midnight at 25%. */
  forestFocusBorder: "rgba(36,36,40,0.25)",
  /** Halo ring around active status dots — success at 18%. */
  forestHalo: "rgba(58,107,69,0.18)",
  /**
   * Brass (`--brass`) — rules and small marks ONLY. It measures 2.94:1 on
   * cream, so it is never type here. It currently has no call sites; if one
   * appears that renders brass TEXT on cream, that site wants `ink`.
   */
  gold: "#B68235",
  /** Info (`--info`) — in_review status. 6.76:1 on cream. */
  info: "#1e52cc",
  /** Mention chip background — info at 12% (`.room-mention`). */
  mentionBg: "rgba(30,82,204,0.12)",
  /** Mentioned-viewer row tint — info at 8% (`.room-message-mentions-me`). */
  mentionRowBg: "rgba(30,82,204,0.08)",

  // Text — midnight mixed into cream, contrast measured against cream.
  /** Primary text (`--midnight`). 15.24:1. */
  ink: "#242428",
  /** Secondary text — midnight 80% into cream. 8.46:1. */
  inkSoft: "#45454b",
  /** Assistant message body text — primary ink. */
  assistantInk: "#242428",
  /** Tertiary / metadata text — midnight 64% into cream. 4.98:1. */
  mute: "#6b6b73",
  /**
   * Disabled text and non-text marks (dots, chevrons, rules) — midnight 50%
   * into cream. 3.28:1, which clears the 3:1 non-text bar but NOT the 4.5:1
   * text floor. Anything that reads as text belongs on `mute`.
   */
  muteSoft: "#8e8e94",
  /** Small uppercase labels — midnight 61% into cream. 4.55:1, the lowest step over AA. */
  sectionCount: "#6b6b73",
  /** Input placeholders — midnight 64% into cream. 4.98:1. */
  placeholder: "#6b6b73",

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
  bannerBg: "#fafafa",
  bannerBorder: "#96501A",
  bannerText: "#96501A",

  // Footer user card — initials avatar
  /** Initials-avatar fill — the one surface step. */
  avatarBg: "#fafafa",
  /** Initials-avatar text — primary ink. */
  avatarText: "#242428",

  // Roster row overlays. These were cream-at-85/90% back when the ground was
  // #faf7f2; on the manual's cream they are cream OVER cream, which composites
  // to exactly the page colour and makes press feedback a no-op. A row that
  // draws no background of its own can only be marked by a surface STEP.
  /** Row press feedback — the one surface step. */
  rowPressed: "#fafafa",
  /** Row selected — the second step, so selected still reads under a press. */
  rowActive: "#ededed",
  /**
   * Active session row background. Was midnight at 8%, which composited to
   * rgb(227,222,211) and dropped the row's 11–12px `mute` metadata to 4.25:1.
   * The surface step keeps it at 4.68:1.
   */
  sessionActive: "#fafafa",
  /** Modal backdrop scrim behind bottom sheets — midnight at 35%. */
  scrim: "rgba(36,36,40,.35)",

  // Board status-chip tints (Teams board cards) — a ≤10% wash of the chip's
  // own state colour, the one fill the manual allows a state colour. Neutral
  // statuses get the surface step instead, because mute is not a state.
  statusTintBacklog: "#fafafa",
  statusTintTodo: "rgba(150,80,26,.10)",
  statusTintProgress: "rgba(150,80,26,.10)",
  statusTintReview: "rgba(53,86,111,.10)",
  statusTintBlocked: "rgba(150,56,43,.10)",
  statusTintCancelled: "#fafafa",
  statusTintDone: "rgba(58,107,69,.10)",

  // Tool-pill status trios — wash + state-coloured type, dot and border.
  tool: {
    running: {
      bg: "rgba(58,107,69,.10)",
      text: "#3A6B45",
      border: "#3A6B45",
      dot: "#3A6B45",
    },
    done: { bg: "#fafafa", text: "#6b6b73", border: "#e4e4e7", dot: "#8e8e94" },
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

  // Presence dot (DM list avatar, mirrors web's ParticipantAvatar/`--presence*`).
  /** Agent presence -- always reachable. Alias of `info`, 6.76:1 on cream. */
  presenceAgent: palette.info,
  /**
   * Online-human presence. Alias of `runningGreen`, 5.44:1 on cream. Not yet
   * wired: RoomMember carries no `online` field on mobile, so this only
   * lights up once that data reaches the app -- see Avatar.tsx.
   */
  presenceOnline: palette.runningGreen,

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
