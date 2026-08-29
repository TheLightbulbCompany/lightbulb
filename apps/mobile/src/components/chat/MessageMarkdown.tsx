import { memo } from "react";
import { Text, View } from "react-native";
import Markdown, {
  MarkdownIt,
  type ASTNode,
  type RenderRules,
} from "react-native-markdown-display";

import { colors, fonts, radii, spacing } from "../../config/theme";

/**
 * Assistant-message markdown (spec §2). Style map mirrors the web
 * MarkdownContent/CodeBlock scale; fenced code gets the bordered
 * header-bar treatment via a custom `fence` rule (no syntax colors v1).
 * Links open externally through the library's default Linking.openURL
 * handler; only the color/underline is ours.
 *
 * Exported memoized: completed transcript segments keep a stable `text`,
 * so only the actively streaming segment re-parses per chunk.
 */

// The library's default instance plus linkify: agents post bare URLs
// constantly, and without linkify they render as dead text (web autolinks
// them via GFM). typographer matches the library default.
const markdownIt = MarkdownIt({ typographer: true, linkify: true });
const rules: RenderRules = {
  // Bordered code card: header bar with the fence's language label, mono
  // body on the code surface (spec §2 fenced code block).
  fence: (node, children, parent, styles) => {
    const lang = (
      (node as ASTNode & { sourceInfo?: string }).sourceInfo ?? ""
    ).trim();
    let { content } = node;
    if (content.endsWith("\n")) {
      content = content.slice(0, -1);
    }
    return (
      <View key={node.key} style={styles._VIEW_SAFE_fenceWrap}>
        <View style={styles._VIEW_SAFE_fenceHeader}>
          <Text style={styles.fenceLang}>{lang || "code"}</Text>
        </View>
        <Text style={styles.fence}>{content}</Text>
      </View>
    );
  },
};

// Plain object, not StyleSheet.create — the library flattens and re-creates
// it internally, merging over its defaults (mergeStyle).
const markdownStyles = {
  body: {
    fontFamily: fonts.sans,
    fontSize: 14,
    lineHeight: 28,
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
  strong: { fontFamily: fonts.sansSemiBold, fontWeight: "normal" as const },
  link: { color: colors.forest, textDecorationLine: "underline" as const },
  bullet_list: { marginBottom: spacing.md },
  ordered_list: { marginBottom: spacing.md },
  list_item: { marginBottom: spacing.xs },
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
    paddingHorizontal: spacing.md,
    paddingVertical: spacing.sm,
    fontFamily: fonts.sansMedium,
  },
  tr: { borderColor: colors.edge },
  td: { paddingHorizontal: spacing.md, paddingVertical: spacing.sm },
  hr: {
    backgroundColor: colors.edge,
    height: 1,
    marginVertical: spacing.lg,
  },
};

function MessageMarkdownInner({ text }: { text: string }) {
  return (
    <Markdown style={markdownStyles} rules={rules} markdownit={markdownIt}>
      {text}
    </Markdown>
  );
}

export const MessageMarkdown = memo(MessageMarkdownInner);
