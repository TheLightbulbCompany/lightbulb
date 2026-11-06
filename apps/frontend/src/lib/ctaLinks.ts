// A draft's call to action whose link isn't known yet (W15-6, W16-5, W17-3): "[**BOOK YOUR SPOT**]([BOOKING LINK])",
// "[Sign up](REGISTRATION_URL)", "[Book]({{link}})", "[Book]()". One pass over the markdown source, before any parser
// sees it, so bold inside the brackets or a bracketed URL can't split it across nodes. Read by the shared renderer
// (MarkdownView) and by Copy (docText `plainCta`).

/** Not a link anyone can follow: empty, "#", a [bracketed] / {{templated}} / <ANGLED> stand-in, or bare words with no
 *  scheme, path or dot ("BOOKING LINK", "registration_url"). `mention:` and other schemes, paths and domains pass. */
export function isPlaceholderUrl(raw: string): boolean {
  const url = raw.trim().replace(/^<(.*)>$/, "$1").trim();
  if (!url || url === "#" || /^\[.*\]$/.test(url) || /^\{.*\}$/.test(url)) return true;
  if (/^[a-z][a-z0-9+.-]*:/i.test(url) || /^[/#?.]/.test(url)) return false;
  return !url.includes(".");
}

const EMPHASIS = /(\*\*|__|\*|_)(.+?)\1/g;
const words = (label: string): string => label.replace(EMPHASIS, "$2").trim();
// [label](url): no nested brackets in the label; the URL may itself be "[…]". Not an image ("![…]").
const LINK = /(!?)\[([^\]\n]+)\]\((\[[^\]\n]*\]|[^()\n]*)\)/g;
// A whole line that is one call to action, maybe bold or a list item: "**[Book](LINK)**", "- [**Book**]".
const CTA_LINE = /^([ \t]*(?:[-*+][ \t]+)?)(\*\*|__)?\[([^\]\n]{2,60})\](?:\((\[[^\]\n]*\]|[^()\n]*)\))?\2?[ \t]*$/;

/** Placeholder links become their words: a line that is only the call to action becomes "[WORDS]" (rendered as a
 *  button-styled label, copied as the words), one inside a sentence becomes plain words. Real links and code are
 *  untouched. */
export function normalizeCtaLinks(markdown: string): string {
  let fenced = false;
  // "[Docs]" on its own line is a real link when the doc defines it ("[Docs]: https://…").
  const defined = new Set([...markdown.matchAll(/^[ \t]{0,3}\[([^\]\n]+)\]:[ \t]*\S/gm)].map((m) => m[1].toLowerCase()));
  return markdown.split("\n").map((line) => {
    if (/^\s*(```|~~~)/.test(line)) { fenced = !fenced; return line; }
    if (fenced) return line;
    const cta = CTA_LINE.exec(line);
    // "[x]" alone is a CTA too (W16-5), but "[ ]" / "[x]" task boxes are 1 character and never match.
    if (cta && (cta[4] === undefined ? !defined.has(cta[3].toLowerCase()) : isPlaceholderUrl(cta[4]))) return `${cta[1]}[${words(cta[3])}]`;
    // Inline code keeps its text: only the parts between `code spans` are read.
    return line.split(/(`+[^`]*`+)/).map((part, i) => i % 2 ? part : part.replace(LINK, (all, bang: string, label: string, url: string) =>
      bang || !isPlaceholderUrl(url) ? all : words(label))).join("");
  }).join("\n");
}

/** An email draft's meta lines ("Preview text: …", "Preheader: …", W18): notes for an email tool, not the email.
 *  Only for what a task made (`resultPiece`, a task's file in the viewer); never a file someone uploaded. */
export function stripMetaLines(text: string): string {
  return text.replace(/^[ \t]*(?:[-*+][ \t]+)?(?:\*\*|__)?(?:preview[ \t]+text|preheader(?:[ \t]+text)?)(?:\*\*|__)?[ \t]*:.*(?:\n|$)/gim, "");
}

/** Copy's text: placeholder links as their words, and a call-to-action line as its words without brackets. */
export function plainCta(text: string): string {
  return normalizeCtaLinks(text).replace(/^([ \t]*(?:[-*+][ \t]+)?)\[([^\]\n]{2,60})\][ \t]*$/gm, "$1$2");
}
