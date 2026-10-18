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
    if (m) out.push({ level: m[1].length as 1 | 2 | 3, text: m[2].replace(/[*_`]/g, "").trim() });
  }
  return out;
}

export const CONTENTS_MIN_HEADINGS = 4;
