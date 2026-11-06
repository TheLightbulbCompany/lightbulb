/** Markdown → one line of plain text for a notification body, which renders no markdown.
 *  Single-asterisk emphasis is left alone: its word-boundary rule differs between JS and
 *  Python for non-Latin text, and a stray `*` costs less than a wrong strip.
 *  Mirrored by `_plain_preview` in the messaging-projector Lambda (push bodies). */
export function plainPreview(markdown: string): string {
  // Bounded first: the rules run on a preview's worth of text, never a whole long message.
  return markdown
    .slice(0, 1000)
    .replace(/^\s*```.*$/gm, "") // code fence lines
    .replace(/!?\[([^\]]*)\]\([^)]*\)/g, "$1") // links and images keep their text
    .replace(/^\s*([-*_])(\s*\1){2,}\s*$/gm, "") // horizontal rules
    .replace(/^\s{0,3}(#{1,6}\s+|>\s?|[-*+]\s+|\d+[.)]\s+)/gm, "") // heading, quote, list markers
    .replace(/\*\*|__|~~|(?<!\\)`/g, "")
    .replace(/\\([*_~`])/g, "$1") // the composer's escapes, after the marks: a typed `x` keeps its backticks
    .replace(/^(\s*)\\([#>+=-])/gm, "$1$2")
    .replace(/^(\s*\d+)\\([.)])/gm, "$1$2")
    .replace(/\s+/g, " ")
    .trim();
}
