import { stripMetaLines } from "@/lib/ctaLinks";

// A doc output's words for people (W13-7, W13 tiles): a readable name from its file name, and its first heading and
// opening lines from its text. Pure, so the viewer header, Made here and Creations read a doc the same way.

/** "autumn-specials-menu-blurbs.md" → "Autumn specials menu blurbs". A name that already has spaces ("Q3 plan v1.2")
 *  keeps its words; only a letter extension comes off. */
export function readableName(fileName: string): string {
  let base = fileName.trim().replace(/\.[a-z][a-z0-9]{0,5}$/i, "");
  if (!/\s/.test(base)) base = base.replace(/[-_]+/g, " ").trim();
  return base ? base[0].toUpperCase() + base.slice(1) : fileName;
}

const TYPE_WORDS: Record<string, string> = { DOC: "Word", DOCX: "Word", XLS: "Excel", XLSX: "Excel", PPT: "PowerPoint", PPTX: "PowerPoint" };

/** A file's type as people say it: "PDF", "Word", "PNG"; "" for a text doc (.md, .txt) or no extension. */
export function fileTypeWord(fileName: string): string {
  const ext = /\.([a-z][a-z0-9]{0,5})$/i.exec(fileName.trim())?.[1].toUpperCase() ?? "";
  return ext === "MD" || ext === "TXT" ? "" : TYPE_WORDS[ext] ?? ext;
}

/** The readable name plus its type (W18-10: a sign-up sheet as a PDF and a .docx read the same): "Sign up sheet · PDF". */
export function fileLabel(fileName: string): string {
  const type = fileTypeWord(fileName);
  return type ? `${readableName(fileName)} · ${type}` : readableName(fileName);
}

/** Markdown marks off one line: list and quote markers, emphasis, code ticks, links to their words. */
const plain = (line: string): string => line
  .replace(/^\s*(?:[-*+]|\d+[.)])\s+/, "").replace(/^\s*>\s?/, "")
  .replace(/!?\[([^\]]*)\]\([^)]*\)/g, "$1").replace(/(\*\*|__|\*|_|`)(.+?)\1/g, "$2").trim();

/** The first heading (any level), and the first `max` lines after it (later headings count as lines), past front
 *  matter, rules and code fences. */
export function docPreview(text: string, max = 3): { heading: string | null; lines: string[] } {
  let heading: string | null = null;
  const lines: string[] = [];
  let fenced = false;
  for (const raw of text.replace(/^---\n[\s\S]*?\n---\n/, "").split("\n")) {
    if (/^\s*(```|~~~)/.test(raw)) { fenced = !fenced; continue; }
    if (fenced || !raw.trim() || /^\s*([-*_])\s*\1\s*\1[\s\-*_]*$/.test(raw)) continue;
    const h = /^\s*#{1,6}\s+(.+?)\s*#*\s*$/.exec(raw);
    if (h && !heading) { heading = plain(h[1]) || null; continue; }
    const line = plain(h ? h[1] : raw);
    if (line && lines.length < max) lines.push(line);
    if (heading && lines.length >= max) break;
  }
  return { heading, lines };
}

/** A short result's name (W16-3): an email's subject, else its first heading or line, at most 80 characters, with the
 *  markdown marks off. */
export function textName(text: string): string {
  const subject = /^\s*\**subject\**\s*:\s*(.+)$/im.exec(text);
  const { heading, lines } = docPreview(text, 1);
  const name = plain(subject?.[1] ?? heading ?? lines[0] ?? "");
  return name.length > 80 ? `${name.slice(0, 79).replace(/\s+\S*$/, "")}…` : name;
}

/** A call-to-action line ("[**Book**]([BOOKING LINK])", shown as a button) as its words, for Copy (W16-5, W17-3). */
export { plainCta } from "@/lib/ctaLinks";

/** The deliverable in a short result: what follows its first paragraph ("Drafted the post." + the post), else null.
 *  The backend's `result_piece` cuts the same way. */
export function resultPiece(text: string): string | null {
  const body = text.trim();
  const cut = body.indexOf("\n\n");
  return cut < 0 ? null : stripMetaLines(body.slice(cut)).trim() || null;
}
