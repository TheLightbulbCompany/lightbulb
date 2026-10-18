export interface OutlineEntry { level: 1 | 2 | 3; text: string }

/** h1–h3 headings in order, ignoring fenced code. The contents list is shown at four or more. */
export function outlineOf(markdown: string): OutlineEntry[] {
  const out: OutlineEntry[] = [];
  let fenced = false;
  for (const raw of markdown.split("\n")) {
    const line = raw.trimEnd();
    if (/^\s*(```|~~~)/.test(line)) { fenced = !fenced; continue; }
    if (fenced) continue;
    const m = /^(#{1,3})\s+(.+?)\s*#*$/.exec(line);
    if (m) out.push({ level: m[1].length as 1 | 2 | 3, text: headingText(m[2]) });
  }
  return out;
}

/** The text react-markdown renders for a heading: link and image syntax reduced to their text, emphasis markers dropped. */
function headingText(raw: string): string {
  return raw
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
    .replace(/[*_`]/g, "")
    .trim();
}

export const CONTENTS_MIN_HEADINGS = 4;
