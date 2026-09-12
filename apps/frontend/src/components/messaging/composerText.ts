export type MarkdownStyle = "bold" | "italic" | "strike" | "code" | "codeblock" | "ul" | "ol" | "link";

const PAIRS: Record<"bold" | "italic" | "strike" | "code", string> = {
  bold: "**",
  italic: "_",
  strike: "~~",
  code: "`",
};

export function wrapSelection(
  value: string,
  start: number,
  end: number,
  style: MarkdownStyle,
): { text: string; selectionStart: number; selectionEnd: number } {
  const selected = value.slice(start, end);
  if (style === "ul" || style === "ol") {
    const lineStart = value.lastIndexOf("\n", Math.max(0, start - 1)) + 1;
    const newline = value.indexOf("\n", end);
    const lineEnd = newline === -1 ? value.length : newline;
    const block = value
      .slice(lineStart, lineEnd)
      .split("\n")
      .map((line, index) => `${style === "ol" ? `${index + 1}. ` : "- "}${line}`)
      .join("\n");
    return {
      text: `${value.slice(0, lineStart)}${block}${value.slice(lineEnd)}`,
      selectionStart: lineStart,
      selectionEnd: lineStart + block.length,
    };
  }
  if (style === "codeblock") {
    return {
      text: `${value.slice(0, start)}\`\`\`\n${selected}\n\`\`\`${value.slice(end)}`,
      selectionStart: start + 4,
      selectionEnd: start + 4 + selected.length,
    };
  }
  if (style === "link") {
    const label = selected || "text";
    const urlStart = start + label.length + 3;
    return {
      text: `${value.slice(0, start)}[${label}](url)${value.slice(end)}`,
      selectionStart: urlStart,
      selectionEnd: urlStart + 3,
    };
  }
  const marker = PAIRS[style];
  if (value.slice(Math.max(0, start - marker.length), start) === marker && value.slice(end, end + marker.length) === marker) {
    return {
      text: `${value.slice(0, start - marker.length)}${selected}${value.slice(end + marker.length)}`,
      selectionStart: start - marker.length,
      selectionEnd: end - marker.length,
    };
  }
  return {
    text: `${value.slice(0, start)}${marker}${selected}${marker}${value.slice(end)}`,
    selectionStart: start + marker.length,
    selectionEnd: end + marker.length,
  };
}
