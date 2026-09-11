export type TriggerKind = "mention" | "emoji";

export interface CompletionTrigger {
  /** "mention" for `@`, "emoji" for `:`. */
  kind: TriggerKind;
  /** Index of the sigil itself. */
  start: number;
  /** The caret. */
  end: number;
  /** Everything typed after the sigil. */
  query: string;
}

// A mention query may carry up to two internal spaces so `@Aden Kim` is
// reachable (Open decision 1: the human token in text is `@Display Name`).
// Nothing here rejects prose — the caller closes the list when no member
// matches, which is the only rule that generalises to every display name.
const MENTION_RE = /(?:^|\s)@([^\s@:]*(?: [^\s@:]*){0,2})$/;
// Two characters minimum, per the spec's "`:` emoji typeahead after two characters".
const EMOJI_RE = /(?:^|\s):([a-z0-9_+-]{2,})$/i;

export function activeTrigger(value: string, caret: number | null): CompletionTrigger | null {
  if (caret === null) return null;
  const head = value.slice(0, caret);
  const emoji = EMOJI_RE.exec(head);
  if (emoji) return { kind: "emoji", start: caret - emoji[1].length - 1, end: caret, query: emoji[1] };
  const mention = MENTION_RE.exec(head);
  if (mention) return { kind: "mention", start: caret - mention[1].length - 1, end: caret, query: mention[1] };
  return null;
}

export function applyCompletion(value: string, trigger: CompletionTrigger, token: string): { text: string; caret: number } {
  const tail = value.slice(trigger.end);
  const separator = tail.startsWith(" ") ? "" : " ";
  return {
    text: `${value.slice(0, trigger.start)}${token}${separator}${tail}`,
    caret: trigger.start + token.length + separator.length,
  };
}

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
