// The search API marks matches with \u0001…\u0002 instead of HTML, so a
// message's own text can never become markup here.
export function snippetParts(snippet: string): { text: string; match: boolean }[] {
  return snippetText(snippet).split(/(\u0001[^\u0002]*\u0002)/).filter(Boolean).map((part) => part.startsWith("\u0001")
    ? { text: part.slice(1, -1), match: true }
    : { text: part, match: false });
}

/** A snippet as it reads, not as it was typed: markdown syntax dropped, the match marks kept.
 * ponytail: regexes for the common marks (links, emphasis, code, headings, lists, quotes);
 * a snippet cut mid-syntax can keep a stray character. */
export function snippetText(snippet: string): string {
  return snippet
    .replace(/!?\[([^\]]*)\]\([^)\s]*\)?/g, "$1") // [text](url) -> text, also when the cut lands in the url
    .replace(/`+/g, "")
    .replace(/(^|\n)[ \t]*(#{1,6}|>|[-*+]|\d+[.)])[ \t]+/g, "$1")
    .replace(/\*\*|__|~~/g, "")
    .replace(/(^|[\s(\u0001])[*_](?=[^*_\s])/g, "$1")
    .replace(/([^*_\s])[*_](?=$|[\s.,;:!?)\u0002])/g, "$1")
    .replace(/\s+/g, " ")
    .trim();
}

export const plainSnippet = (snippet: string) => snippetText(snippet).replace(/[\u0001\u0002]/g, "");

export function SearchSnippet({ snippet }: { snippet: string }) {
  return <>{snippetParts(snippet).map((part, index) => part.match
    ? <mark key={index} className="rounded-[2px] bg-[color-mix(in_srgb,var(--link)_22%,transparent)] text-inherit">{part.text}</mark>
    : part.text)}</>;
}
