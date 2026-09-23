/** Markdown → one line of plain text for a notification body, which renders no markdown.
 *  Mirrored by `_plain_preview` in the messaging-projector Lambda (push bodies). */
export function plainPreview(markdown: string): string {
  return markdown
    .replace(/^\s*```.*$/gm, "") // code fence lines
    .replace(/!?\[([^\]]*)\]\([^)]*\)/g, "$1") // links and images keep their text
    .replace(/^\s*([-*_])(\s*\1){2,}\s*$/gm, "") // horizontal rules
    .replace(/^\s{0,3}(#{1,6}\s+|>\s?|[-*+]\s+|\d+[.)]\s+)/gm, "") // heading, quote, list markers
    .replace(/\*\*|__|~~|`/g, "")
    .replace(/(^|[^\w*])\*(\S(?:.*?\S)?)\*(?!\w)/g, "$1$2") // *emphasis*
    .replace(/\s+/g, " ")
    .trim();
}
