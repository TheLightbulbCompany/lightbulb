/**
 * Design tokens for the mobile app (RN has no CSS variables; this is the
 * StyleSheet-era stand-in). ALL color literals live here: screens import
 * tokens, never raw colors.
 *
 * The app is NEUTRAL: white and grey, not cream. The web moved to the quiet
 * chrome on 2026-09-12 (#2547 / #2536) and the phone followed.
 *
 * Two sets live in this file:
 *
 * - `chatPalette` / `useChatPalette()`: the current one, light AND dark. It
 *   mirrors the plane ladder in the web's `globals.css`
 *   (`:root[data-chat-theme]`): ground, surface, raised, edge, three text
 *   steps, link / alert / live. Every redesigned screen draws from it, and a
 *   new screen should too.
 * - `colors`: the older, LIGHT-ONLY set. Its key names (`cream`,
 *   `creamDeeper`, `midnight`, ...) predate the change and are only names: the
 *   values are neutral (`cream` is `#ffffff`), though not the ladder's exact
 *   ones. A screen still on `colors` has no dark mode until it moves to
 *   `chatPalette`.
 *
 * The brand marks did not change: bulb #F0C34E (the light: never type, never
 * a fill) and brass #B68235 (3.37:1 on white, so never type).
 *
 * The text steps in `colors` (`#242428`, `#45454b`, `#6b6b73`, `#8e8e94`) are
 * this older set's own. They are NOT the web's current steps (`--fg`
 * `#1A1A1C`, `--fg-soft` `#52525A`, `--fg-mute` `#606069`), which
 * `chatPalette` carries. Each carries its contrast on white, by the WCAG
 * formula, recomputed 2026-09-28: the figures here before were the old cream
 * palette's.
 *
 * State rules (§06): a state colour marks text, an icon, a border or a thin
 * rule — never a large fill. Two exceptions: a ≤10% alpha wash behind a chip
 * or banner (what the `rgba(…,.10)` tints below are), and THE destructive
 * confirm, which may be solid `danger` the way web's `--destructive` button
 * is — one per sheet, never a decorative fill.
 */
import { Platform, useColorScheme } from "react-native";
import type { TextStyle } from "react-native";

/** The manual's palette. Key names predate the rebrand; the values are current. */
const palette = {
  // Surfaces
  /** Page / main background. White; `cream` is only the key's old name. */
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
   * The same white as `cream` above. Cards are delineated by an `edge`
   * border, not by a surface shift. Kept as its own key because ~98 call
   * sites read it, including button labels on the dark CTA.
   */
  white: "#ffffff",

  // Borders
  /** Hairline borders on white (`--edge`). */
  edge: "#e4e4e7",
  /** Softer separation on white (`--edge-soft`). */
  edgeSoft: "#f0f0f2",
  /** Was a forest-tinted hairline; the manual has one hairline (`--edge`). */
  edgeForest: "#e4e4e7",

  // Brand
  /** Midnight — primary CTA, links, focus (`--midnight`). */
  forest: "#242428",
  /** Success — active status dots (`--ok`). 6.23:1 on white. */
  forestSoft: "#3A6B45",
  /** Brand tint surface — monogram bg. The one surface step. */
  forestTint: "#fafafa",
  /** Composer focus ring — midnight at 25%. */
  forestFocusBorder: "rgba(36,36,40,0.25)",
  /** Halo ring around active status dots — success at 18%. */
  forestHalo: "rgba(58,107,69,0.18)",
  /**
   * Brass (`--brass`) — rules and small marks ONLY. It measures 3.37:1 on
   * white, so it is never type here. It currently has no call sites; if one
   * appears that renders brass TEXT on white, that site wants `ink`.
   */
  gold: "#B68235",
  /** Info (`--info`) — in_review status. 6.70:1 on white. */
  info: "#1e52cc",
  /** Mention chip background — info at 12% (`.room-mention`). */
  mentionBg: "rgba(30,82,204,0.12)",
  /** Mentioned-viewer row tint — info at 8% (`.room-message-mentions-me`). */
  mentionRowBg: "rgba(30,82,204,0.08)",

  // Text steps; contrast is on white.
  /** Primary text. 15.46:1. */
  ink: "#242428",
  /** Secondary text. 9.52:1. */
  inkSoft: "#45454b",
  /** Assistant message body text — primary ink. */
  assistantInk: "#242428",
  /** Tertiary / metadata text. 5.28:1. */
  mute: "#6b6b73",
  /**
   * Disabled text and non-text marks (dots, chevrons, rules). 3.26:1, which
   * clears the 3:1 non-text bar but NOT the 4.5:1 text floor. Anything that
   * reads as text belongs on `mute`.
   */
  muteSoft: "#8e8e94",
  /** Small uppercase labels — the same step as `mute`. 5.28:1. */
  sectionCount: "#6b6b73",
  /** Input placeholders — the same step as `mute`. 5.28:1. */
  placeholder: "#6b6b73",

  // Status — the manual's state pairs, light-ground halves (§06).
  /** Warning / thinking (`--warn`). 6.07:1. */
  warn: "#96501A",
  /** Destructive / error (`--err`). 7.25:1. */
  danger: "#96382B",
  /** Success / connected (`--ok`). 6.23:1. */
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

  // Roster row overlays. A tint of the ground's own colour laid over the
  // ground composites to exactly the page colour and makes press feedback a
  // no-op. A row that draws no background of its own can only be marked by a
  // surface STEP.
  /**
   * Row press feedback — a REAL step, not the one-surface hover tint. Was
   * `#fafafa` (Δ from white: 5,5,5 → ~2%, invisible on iOS — Slack darkens a
   * pressed row unmistakably). Now the same grey as `edge`: Δ from white
   * 27,27,24 → 10.6% / 10.6% / 9.4% (min 9.4%), clearing the ≥8% floor and
   * dark enough that `rowActive` still reads as a distinct, lighter step
   * underneath a press.
   */
  rowPressed: "#e4e4e7",
  /** Row selected — the second step, so selected still reads under a press. */
  rowActive: "#ededed",
  /**
   * Active session row background: the surface step, not a tint of ink,
   * which dims the row's 11–12px `mute` metadata. On this step `mute`
   * measures 5.06:1.
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
  /** Agent presence -- always reachable. Alias of `info`, 6.70:1 on white. */
  presenceAgent: palette.info,
  /**
   * Online-human presence. Alias of `runningGreen`, 6.23:1 on white. Not yet
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
 * Mirrors the web (`globals.css`): Satoshi carries UI chrome, display type and
 * the wordmark; Inter is reserved for reading surfaces (message bodies) so
 * controls and message copy stay visually distinct; platform mono for code.
 * The `lora*` keys are compatibility aliases onto Satoshi, exactly as the web
 * keeps `--font-lora-serif` pointing at `--font-sans`.
 */
export const fonts = {
  sans: "Satoshi-Regular",
  sansMedium: "Satoshi-Medium",
  // ponytail: Satoshi ships no static 600 and the licence forbids
  // distributing an instance cut from the variable font, so semibold uses the
  // official Bold. Swap when Fontshare publishes a 600 face.
  sansSemiBold: "Satoshi-Bold",
  lora: "Satoshi-Regular",
  /** Display headings and the wordmark (web: Satoshi 500). */
  loraMedium: "Satoshi-Medium",
  /** Italic — taglines, agent vibe lines. */
  loraItalic: "Satoshi-Italic",
  /** Agent monograms. */
  loraBoldItalic: "Satoshi-BoldItalic",
  /** Reading text — message bodies (web: `--font-body`). Satoshi too: one face everywhere. */
  body: "Satoshi-Regular",
  bodySemiBold: "Satoshi-Bold",
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
  /**
   * Navigation chrome — a custom `headerTitle`, and anything else drawn INSIDE
   * a native nav bar. Deliberately carries NO `fontFamily`: an iOS nav bar has
   * to draw in the system font (SF) or the whole screen reads as a web view,
   * and 17/600 is what UIKit itself uses for a title. Brand fonts stay in
   * CONTENT — this is the one exemption, and it is a subtraction, not a literal.
   */
  navTitle: {
    fontSize: 17,
    lineHeight: 22,
    fontWeight: "600",
    color: colors.ink,
  },
  /** The second line of a two-line nav title (member counts and the like). */
  navMeta: {
    fontSize: 11,
    lineHeight: 14,
    color: colors.mute,
  },
  /** Small uppercase labels — Satoshi 500, 10px, tracked 0.19em (§03/§06). */
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

/**
 * The messaging palette: web `globals.css` `:root[data-chat-theme]` and its
 * `="dark"` override, one-for-one (the iPhone redesign's tokens). `colors`
 * above stays the light-only palette the not-yet-redesigned screens read.
 *
 * Four aliases name ROLES, never new colours — each is a value already in the
 * same scheme: `ground` (conversation/grouped-list background: light sidebar,
 * dark rail), `surface` (rows, bubbles, cards: pane), `chip` (hanging chips:
 * light selected, dark raised) and `onFg` (text on an fg-filled bubble or
 * button: light pane, dark rail).
 *
 * Contrast: every text token is checked against EVERY plane
 * (`__tests__/chatPalette.test.ts`), the web rule.
 */
const chatLight = {
  rail: "#DEDEDE",
  sidebar: "#EFEFEF",
  pane: "#FFFFFF",
  raised: "#F7F7F7",
  edge: "#E2E2E2",
  selected: "#E4E4E6",
  hover: "#F2F2F3",
  fg: "#1A1A1C",
  fgSoft: "#52525A",
  fgMute: "#606069",
  link: "#1E52CC",
  onLink: "#FFFFFF",
  alert: "#A83C29",
  live: "#186D48",
  warn: "#52525A",
  agentTint: "#6E6E78",
  onAgentTint: "#FFFFFF",
  ground: "#EFEFEF",
  surface: "#FFFFFF",
  chip: "#E4E4E6",
  onFg: "#FFFFFF",
} as const;

export type ChatPalette = { readonly [K in keyof typeof chatLight]: string };

export const chatPalette: { light: ChatPalette; dark: ChatPalette } = {
  light: chatLight,
  dark: {
    rail: "#101012",
    sidebar: "#171719",
    pane: "#1E1E21",
    raised: "#27272B",
    edge: "#323236",
    selected: "#2E2E33",
    hover: "#26262A",
    fg: "#ECECEE",
    fgSoft: "#B4B4B8",
    fgMute: "#97979C",
    link: "#8FB0FF",
    onLink: "#0C121A",
    alert: "#E7806F",
    live: "#4BC08A",
    warn: "#B4B4B8",
    agentTint: "#9B9BA2",
    onAgentTint: "#101012",
    ground: "#101012",
    surface: "#1E1E21",
    chip: "#27272B",
    onFg: "#101012",
  },
};

/** The messaging palette for the phone's current appearance (app.json is `automatic`). */
export function useChatPalette(): ChatPalette {
  return useColorScheme() === "dark" ? chatPalette.dark : chatPalette.light;
}
