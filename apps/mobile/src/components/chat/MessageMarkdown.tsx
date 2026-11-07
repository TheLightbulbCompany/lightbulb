import { CARD_FENCE, parseMentionHref } from "@lightbulb/shared";
import { memo, useMemo } from "react";
import { Linking, ScrollView, Text, View } from "react-native";
import { router } from "expo-router";
import Markdown, {
  MarkdownIt,
  renderRules,
  type ASTNode,
  type RenderRules,
} from "react-native-markdown-display";

import { WEB_ORIGIN } from "../../config/domain";
import { activityMobileHref } from "../../lib/activityLink";
import { sessionMobileHref } from "../../lib/sessionLink";
import { colors, fonts, radii, spacing, type ChatPalette } from "../../config/theme";
import { useCardReply } from "./openui/CardReplyContext";
import { OpenUICard } from "./openui/OpenUICard";

/**
 * Assistant-message markdown (spec §2). Style map mirrors the web
 * MarkdownContent/CodeBlock scale; fenced code gets the bordered
 * header-bar treatment via a custom `fence` rule (no syntax colors v1).
 * Text groups and code blocks opt into React Native's native granular
 * selection; links continue to open through the library's default
 * Linking.openURL handler. A `mention:` href (written by
 * the shared `linkifyMentions` grammar) instead renders as an inert chip via
 * a custom `link` rule.
 *
 * Exported memoized: completed transcript segments keep a stable `text`,
 * so only the actively streaming segment re-parses per chunk.
 */

// The library's default instance plus linkify: agents post bare URLs
// constantly, and without linkify they render as dead text (web autolinks
// them via GFM). typographer matches the library default.
const markdownIt = MarkdownIt({ typographer: true, linkify: true });
const rules: RenderRules = {
  // `selectable` belongs on one outer Text tree so the native iOS/Android
  // selection handles can cross inline markdown spans. Keeping links as nested
  // Text nodes preserves their existing tap handler and accessibility role.
  textgroup: (node, children, parent, styles) => (
    <Text key={node.key} style={styles.textgroup} selectable>
      {children}
    </Text>
  ),
  // Indented code blocks do not pass through `textgroup`, so opt their own
  // Text node into the same platform-native selection behavior.
  code_block: (node, children, parent, styles, inheritedStyles = {}) => {
    const content = node.content.endsWith("\n")
      ? node.content.slice(0, -1)
      : node.content;
    return (
      <Text key={node.key} style={[inheritedStyles, styles.code_block]} selectable>
        {content}
      </Text>
    );
  },
  // Bordered code card: header bar with the fence's language label, mono
  // body on the code surface (spec §2 fenced code block).
  fence: (node, children, parent, styles) => {
    const lang = (
      (node as ASTNode & { sourceInfo?: string }).sourceInfo ?? ""
    ).trim();
    const content = node.content.endsWith("\n")
      ? node.content.slice(0, -1)
      : node.content;
    return (
      <View key={node.key} style={styles._VIEW_SAFE_fenceWrap}>
        <View style={styles._VIEW_SAFE_fenceHeader}>
          <Text style={styles.fenceLang}>{lang || "code"}</Text>
        </View>
        {/* flexGrow 0: a ScrollView grows by default, and inside a bubble sized
            to its content (alignSelf flex-start) that grew the block tens of
            thousands of points tall -- a blank screen where the message was. */}
        <ScrollView horizontal showsHorizontalScrollIndicator={false} style={horizontalBlockScroll}>
          <Text style={styles.fence} selectable>{content}</Text>
        </ScrollView>
      </View>
    );
  },
  // The library divides table rows into equal flex cells. On a phone that
  // squeezes multi-column prose into a character-wide, screen-tall column.
  table: (node, children, parent, styles) => (
    <ScrollView key={node.key} horizontal showsHorizontalScrollIndicator={false} style={horizontalBlockScroll}>
      <View style={styles._VIEW_SAFE_table}>{children}</View>
    </ScrollView>
  ),
  // Mentions ride the same markdown-link grammar as the web (spec: one
  // grammar for both readers). `linkifyMentions` (called by the caller before
  // this text ever reaches us) rewrites a member token into
  // `[@token](mention:kind:id)`; a href that parses as a mention renders as an
  // inert chip instead of a link. Everything else — including a bare-URL
  // autolink from `linkify: true` — falls through to the library's own `link`
  // rule unchanged, so it still opens via `Linking.openURL`.
  link: (node, children, parent, styles, onLinkPress) => {
    const href = (node.attributes as { href?: string }).href;
    const mention = href ? parseMentionHref(href) : null;
    if (!mention) {
      const sessionHref = sessionMobileHref(href);
      if (sessionHref) {
        return <Text key={node.key} style={styles.link} onPress={() => router.push(sessionHref)}>{children}</Text>;
      }
      // An agent's "add the sign-in here" link: the Activity tab has the form.
      const activityHref = activityMobileHref(href);
      if (activityHref) {
        return <Text key={node.key} style={styles.link} onPress={() => router.push(activityHref)}>{children}</Text>;
      }
      // A relative href (e.g. an agent-written Paperclip path like
      // `/ISOA/agents/cto`, written by the bundled paperclip skill) has no
      // scheme, so Linking.openURL can't open it — it silently does
      // nothing. Resolve it against the web workspace, whose middleware
      // redirects Paperclip-shaped paths onto the in-shell panel
      // (apps/frontend/src/middleware.ts) — mobile has no matching detail
      // screen for this kind yet.
      if (href?.startsWith("/")) {
        return (
          <Text key={node.key} style={styles.link} onPress={() => Linking.openURL(`${WEB_ORIGIN}${href}`)}>
            {children}
          </Text>
        );
      }
      return renderRules.link?.(node, children, parent, styles, onLinkPress) ?? null;
    }
    // Not `{children}`: AstRenderer computes a leaf `text` node's inherited
    // style from its ancestor chain's AST *type* ("link"), before this rule
    // ever runs — so the already-rendered children would still carry the
    // link color/underline no matter what style we hand the wrapper here.
    // A mention token is always flat text (no nested markdown), so reading
    // it straight off the AST sidesteps that inherited styling entirely.
    const label = node.children.map((child) => child.content).join("");
    return (
      <Text key={node.key} style={styles.mention}>
        {label}
      </Text>
    );
  },
};

/** With a card context (an agent's message) the card fence draws as a card; anywhere else it stays code. */
const cardRules = (palette: ChatPalette): RenderRules => ({
  ...rules,
  fence: (node, children, parent, styles, ...rest) => {
    const lang = ((node as ASTNode & { sourceInfo?: string }).sourceInfo ?? "").trim();
    if (lang !== CARD_FENCE) return rules.fence!(node, children, parent, styles, ...rest);
    const content = node.content.endsWith("\n") ? node.content.slice(0, -1) : node.content;
    return <OpenUICard key={node.key} program={content} palette={palette} />;
  },
});

const horizontalBlockScroll = { flexGrow: 0, maxWidth: "100%" as const };

// The library's `flex: 1` gives a list item's text a zero flex basis, so a
// bubble sized to its content ignored list text when choosing its width (it
// came out as wide as its widest heading) while its height was taken at the
// full width: items wrapped to more lines than they had room for and printed
// over each other. Basis auto + shrink lets the text count toward the width.
const listContent = { flex: 0, flexShrink: 1 };

// Plain object, not StyleSheet.create — the library flattens and re-creates
// it internally, merging over its defaults (mergeStyle).
const markdownStyles = {
  body: {
    fontFamily: fonts.body,
    fontSize: 15,
    lineHeight: 22,
    color: colors.assistantInk,
  },
  heading1: {
    fontFamily: fonts.sansSemiBold,
    fontSize: 18,
    lineHeight: 24,
    color: colors.assistantInk,
    marginTop: spacing.lg,
    marginBottom: spacing.sm,
  },
  heading2: {
    fontFamily: fonts.sansSemiBold,
    fontSize: 16,
    lineHeight: 22,
    color: colors.assistantInk,
    marginTop: spacing.md,
    marginBottom: spacing.sm,
  },
  heading3: {
    fontFamily: fonts.sansSemiBold,
    fontSize: 14,
    lineHeight: 20,
    color: colors.assistantInk,
    marginTop: spacing.md,
    marginBottom: spacing.xs,
  },
  heading4: {
    fontFamily: fonts.sansMedium,
    fontSize: 14,
    lineHeight: 20,
    color: colors.assistantInk,
    marginTop: spacing.sm,
    marginBottom: spacing.xs,
  },
  paragraph: { marginTop: 0, marginBottom: spacing.md },
  // Static weight-specific font files: neutralize the default synthetic
  // bold so iOS doesn't look for a bold face inside the 700 family.
  strong: { fontFamily: fonts.bodySemiBold, fontWeight: "normal" as const },
  link: { color: colors.forest, textDecorationLine: "underline" as const },
  // `.room-mention` on web: info text on a 12% info wash, no underline, not
  // tappable (the `link` rule above renders it with no onPress).
  mention: {
    color: colors.info,
    backgroundColor: colors.mentionBg,
    borderRadius: radii.xs,
    paddingHorizontal: spacing.xs,
    fontFamily: fonts.bodySemiBold,
  },
  bullet_list: { marginBottom: spacing.md },
  ordered_list: { marginBottom: spacing.md },
  list_item: { marginBottom: spacing.xs },
  bullet_list_content: listContent,
  ordered_list_content: listContent,
  bullet_list_icon: {
    fontFamily: fonts.sans,
    fontSize: 14,
    lineHeight: 28,
    color: colors.assistantInk,
    marginLeft: spacing.sm,
    marginRight: spacing.sm,
  },
  ordered_list_icon: {
    fontFamily: fonts.sans,
    fontSize: 14,
    lineHeight: 28,
    color: colors.assistantInk,
    marginLeft: spacing.sm,
    marginRight: spacing.sm,
  },
  blockquote: {
    backgroundColor: "transparent",
    borderColor: colors.edge,
    borderLeftWidth: 2,
    marginLeft: 0,
    paddingLeft: spacing.lg,
    paddingRight: 0,
    marginVertical: spacing.md,
    color: colors.mute,
  },
  code_inline: {
    backgroundColor: colors.chip,
    borderWidth: 0,
    borderRadius: radii.xs,
    paddingHorizontal: 6,
    paddingVertical: 2,
    fontFamily: fonts.mono,
    fontSize: 14,
    color: colors.ink,
  },
  code_block: {
    backgroundColor: colors.codeBg,
    borderColor: colors.edge,
    borderRadius: radii.md,
    padding: spacing.md,
    fontFamily: fonts.mono,
    fontSize: 12,
    lineHeight: 18,
    color: colors.assistantInk,
  },
  // Fence body — the wrapper card (border/radius/header) comes from the
  // custom rule's fenceWrap/fenceHeader styles below.
  fence: {
    backgroundColor: colors.codeBg,
    borderWidth: 0,
    borderRadius: 0,
    padding: spacing.md,
    fontFamily: fonts.mono,
    fontSize: 12,
    lineHeight: 18,
    color: colors.assistantInk,
  },
  fenceWrap: {
    marginVertical: spacing.lg,
    borderWidth: 1,
    borderColor: colors.edge,
    borderRadius: radii.md,
    overflow: "hidden" as const,
    backgroundColor: colors.codeBg,
  },
  fenceHeader: {
    paddingHorizontal: spacing.lg,
    paddingVertical: spacing.sm,
    backgroundColor: colors.creamDeeper,
    borderBottomWidth: 1,
    borderBottomColor: colors.edge,
  },
  fenceLang: {
    fontFamily: fonts.mono,
    fontSize: 12,
    lineHeight: 16,
    color: colors.mute,
  },
  table: {
    borderWidth: 1,
    borderColor: colors.edge,
    borderRadius: radii.sm,
    marginBottom: spacing.md,
  },
  thead: { backgroundColor: colors.creamDeeper },
  th: {
    minWidth: 120,
    paddingHorizontal: spacing.md,
    paddingVertical: spacing.sm,
    fontFamily: fonts.sansMedium,
  },
  tr: { borderColor: colors.edge },
  td: { minWidth: 120, paddingHorizontal: spacing.md, paddingVertical: spacing.sm },
  hr: {
    backgroundColor: colors.edge,
    height: 1,
    marginVertical: spacing.lg,
  },
};

/**
 * The bubble-conversation tones (Muse redesign): recolors the same markdown
 * shapes onto `ChatPalette` instead of the legacy `colors` scale, and flips
 * text/link/mention/code-chip colors for a mine (`inverse`) bubble so they
 * stay legible on the `fg` fill. Same key set as `markdownStyles` above.
 */
function bubbleStyles(p: ChatPalette, inverse: boolean) {
  const ink = inverse ? p.onFg : p.fg;
  return {
    // Blocks are spaced by the body's `gap`, not their own margins: a margin
    // under the last block left every bubble with a blank line at the bottom.
    body: { fontFamily: fonts.body, fontSize: 16, lineHeight: 22, color: ink, gap: spacing.md },
    heading1: {
      fontFamily: fonts.sansSemiBold,
      fontSize: 18,
      lineHeight: 24,
      color: ink,
      marginTop: 0,
      marginBottom: -spacing.xs,
    },
    heading2: {
      fontFamily: fonts.sansSemiBold,
      fontSize: 17,
      lineHeight: 23,
      color: ink,
      marginTop: 0,
      marginBottom: -spacing.xs,
    },
    heading3: {
      fontFamily: fonts.sansSemiBold,
      fontSize: 16,
      lineHeight: 22,
      color: ink,
      marginTop: 0,
      marginBottom: -spacing.xs,
    },
    heading4: {
      fontFamily: fonts.sansSemiBold,
      fontSize: 16,
      lineHeight: 22,
      color: ink,
      marginTop: 0,
      marginBottom: -spacing.xs,
    },
    paragraph: { marginTop: 0, marginBottom: 0 },
    strong: { fontFamily: fonts.bodySemiBold, fontWeight: "normal" as const },
    link: {
      color: inverse ? p.onFg : p.link,
      textDecorationLine: "underline" as const,
    },
    mention: {
      color: inverse ? p.onFg : p.link,
      fontFamily: fonts.bodySemiBold,
    },
    bullet_list: { marginBottom: 0 },
    ordered_list: { marginBottom: 0 },
    list_item: { marginBottom: spacing.xs },
    bullet_list_content: listContent,
    ordered_list_content: listContent,
    bullet_list_icon: {
      fontFamily: fonts.sans,
      fontSize: 14,
      lineHeight: 28,
      color: ink,
      marginLeft: spacing.sm,
      marginRight: spacing.sm,
    },
    ordered_list_icon: {
      fontFamily: fonts.sans,
      fontSize: 14,
      lineHeight: 28,
      color: ink,
      marginLeft: spacing.sm,
      marginRight: spacing.sm,
    },
    blockquote: {
      backgroundColor: "transparent",
      borderColor: p.edge,
      borderLeftWidth: 2,
      marginLeft: 0,
      paddingLeft: spacing.lg,
      paddingRight: 0,
      marginVertical: 0,
      color: p.fgSoft,
    },
    code_inline: {
      backgroundColor: inverse ? p.fgSoft : p.raised,
      borderWidth: 0,
      borderRadius: radii.xs,
      paddingHorizontal: 6,
      paddingVertical: 2,
      fontFamily: fonts.mono,
      fontSize: 14,
      color: inverse ? p.onFg : p.fg,
    },
    code_block: {
      backgroundColor: p.raised,
      borderColor: p.edge,
      borderRadius: radii.lg,
      padding: spacing.md,
      fontFamily: fonts.mono,
      fontSize: 13,
      lineHeight: 19,
      color: p.fg,
    },
    fence: {
      backgroundColor: p.raised,
      borderWidth: 0,
      borderRadius: 0,
      padding: spacing.md,
      fontFamily: fonts.mono,
      fontSize: 13,
      lineHeight: 19,
      color: p.fg,
    },
    fenceWrap: {
      marginVertical: 0,
      borderWidth: 1,
      borderColor: p.edge,
      borderRadius: radii.lg,
      overflow: "hidden" as const,
      backgroundColor: p.raised,
    },
    fenceHeader: {
      paddingHorizontal: spacing.lg,
      paddingVertical: spacing.sm,
      backgroundColor: p.raised,
      borderBottomWidth: 1,
      borderBottomColor: p.edge,
    },
    fenceLang: {
      fontFamily: fonts.sansMedium,
      fontSize: 12,
      lineHeight: 16,
      color: p.fgMute,
    },
    table: {
      borderWidth: 1,
      borderColor: p.edge,
      borderRadius: radii.sm,
      marginBottom: 0,
    },
    thead: { backgroundColor: p.raised },
    th: {
      minWidth: 120,
      paddingHorizontal: spacing.md,
      paddingVertical: spacing.sm,
      fontFamily: fonts.sansMedium,
    },
    tr: { borderColor: p.edge },
    td: { minWidth: 120, paddingHorizontal: spacing.md, paddingVertical: spacing.sm },
    hr: { backgroundColor: p.edge, height: 1, marginVertical: spacing.xs },
  };
}

// Keyed by ChatPalette identity (chatPalette.light / chatPalette.dark are
// stable module-level objects), so a scroll re-render never rebuilds styles.
const bubbleStylesCache = new WeakMap<
  ChatPalette,
  { plain: ReturnType<typeof bubbleStyles>; inverse: ReturnType<typeof bubbleStyles> }
>();

function cachedBubbleStyles(palette: ChatPalette, inverse: boolean) {
  let entry = bubbleStylesCache.get(palette);
  if (!entry) {
    entry = { plain: bubbleStyles(palette, false), inverse: bubbleStyles(palette, true) };
    bubbleStylesCache.set(palette, entry);
  }
  return inverse ? entry.inverse : entry.plain;
}

function MessageMarkdownInner({
  text,
  palette,
  inverse = false,
}: {
  text: string;
  palette?: ChatPalette;
  inverse?: boolean;
}) {
  const style = palette ? cachedBubbleStyles(palette, inverse) : markdownStyles;
  const live = useCardReply() !== null;
  const r = useMemo(() => (live && palette ? cardRules(palette) : rules), [live, palette]);
  return (
    <Markdown style={style} rules={r} markdownit={markdownIt}>
      {text}
    </Markdown>
  );
}

export const MessageMarkdown = memo(MessageMarkdownInner);
