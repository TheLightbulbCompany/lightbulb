import { unescapeTyped } from "@lightbulb/shared";

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
  return unescapeTyped(snippet
    .replace(/!?\[([^\]]*)\]\([^)\s]*\)?/g, "$1") // [text](url) -> text, also when the cut lands in the url
    .replace(/(?<!\\)`+/g, "")
    .replace(/(^|\n)[ \t]*(#{1,6}|>|[-*+]|\d+[.)])[ \t]+/g, "$1")
    .replace(/\*\*|__|~~/g, "")
    .replace(/(^|[\s(\u0001])[*_](?=[^*_\s])/g, "$1")
    .replace(/([^*_\s\\])[*_](?=$|[\s.,;:!?)\u0002])/g, "$1"))
    .replace(/\s+/g, " ")
    .trim();
}

/** A snippet that opens on the match (W14: "caraway" read "ort email newsletter announcing…" with the word cut off
 *  at the row's end): a fragment the index cut mid-word ("ort email…", lowercase first) starts at its next word, and
 *  one with more than `before` characters ahead of the match starts at the word boundary that leaves `before` of
 *  context. Either cut is marked "…". The match marks are kept. */
export function snippetAround(snippet: string, before = 28): string {
  let text = snippetText(snippet);
  let cut = false;
  const first = text.indexOf("\u0001");
  if (first !== 0 && /^[a-z]/.test(text)) {
    const space = text.search(/\s/);
    if (space > 0 && (first < 0 || space < first)) { text = text.slice(space + 1); cut = true; }
  }
  const at = text.indexOf("\u0001");
  if (at > before) {
    const space = text.indexOf(" ", at - before);
    text = text.slice(space >= 0 && space < at ? space + 1 : at);
    cut = true;
  }
  return cut ? `…${text}` : text;
}

export const plainSnippet = (snippet: string) => snippetText(snippet).replace(/[\u0001\u0002]/g, "");

export function SearchSnippet({ snippet, around = false }: { snippet: string; around?: boolean }) {
  return <>{snippetParts(around ? snippetAround(snippet) : snippet).map((part, index) => part.match
    ? <mark key={index} className="rounded-[2px] bg-[color-mix(in_srgb,var(--link)_22%,transparent)] text-inherit">{part.text}</mark>
    : part.text)}</>;
}
