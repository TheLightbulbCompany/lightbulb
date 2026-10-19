// Ported from the Paperclip fork we deploy (TheLightbulbCompany/paperclip):
//   packages/shared/src/document-anchors.ts @ b7545823bec49b4a73c3813d8fb634f68fc84b48
// The server verifies every new annotation with `verifyDocumentAnchorSelector`
// against ITS markdown→text projection and 422s on any mismatch, so the client
// must build the selector with the same projection. Keep this byte-for-byte
// with the fork (only the selector-creation half is ported; remap stays
// server-side). If the fork's projection changes, re-port it.

import type { AnnotationAnchorSelector } from "./documentReviewTypes";

export interface DocumentTextPosition {
  sourceStart: number;
  sourceEnd: number;
}

export interface DocumentTextProjection {
  source: string;
  text: string;
  positions: DocumentTextPosition[];
}

export interface DocumentTextRange {
  text: string;
  normalizedStart: number;
  normalizedEnd: number;
  markdownStart: number;
  markdownEnd: number;
}

export interface CreateDocumentAnchorSelectorOptions {
  contextLength?: number;
}

export interface VerifyDocumentAnchorSelectorInput {
  markdown: string;
  selector: AnnotationAnchorSelector;
  contextLength?: number;
}

export interface VerifyDocumentAnchorSelectorResult {
  ok: boolean;
  projection: DocumentTextProjection;
  reason: "verified" | "quote_mismatch" | "position_mismatch" | "invalid_range";
}

const DEFAULT_CONTEXT_LENGTH = 48;

export function normalizeAnchorText(value: string): string {
  return value.replace(/\s+/g, " ").trim();
}

export function projectMarkdownToText(markdown: string): DocumentTextProjection {
  const builder = new ProjectionBuilder(markdown);
  const lines = markdown.match(/[^\n]*(?:\n|$)/g) ?? [markdown];
  let offset = 0;
  let inFence = false;

  for (const rawLine of lines) {
    if (rawLine === "") continue;
    const hasNewline = rawLine.endsWith("\n");
    const line = hasNewline ? rawLine.slice(0, -1) : rawLine;
    const fenceMatch = line.match(/^\s*(```+|~~~+)/);

    if (fenceMatch) {
      inFence = !inFence;
      offset += rawLine.length;
      builder.addSeparator(offset - (hasNewline ? 1 : 0));
      continue;
    }

    if (inFence) {
      builder.addText(line, offset);
      builder.addSeparator(offset + line.length);
      offset += rawLine.length;
      continue;
    }

    const { text, sourceOffset } = stripBlockSyntax(line, offset);
    addInlineMarkdownText(builder, text, sourceOffset);
    builder.addSeparator(offset + line.length);
    offset += rawLine.length;
  }

  return builder.toProjection();
}

export function resolveProjectionRange(
  projection: DocumentTextProjection,
  normalizedStart: number,
  normalizedEnd: number,
): DocumentTextRange | null {
  if (
    normalizedStart < 0
    || normalizedEnd <= normalizedStart
    || normalizedEnd > projection.text.length
    || normalizedStart >= projection.positions.length
    || normalizedEnd - 1 >= projection.positions.length
  ) {
    return null;
  }

  return {
    text: projection.text.slice(normalizedStart, normalizedEnd),
    normalizedStart,
    normalizedEnd,
    markdownStart: projection.positions[normalizedStart]?.sourceStart ?? 0,
    markdownEnd: projection.positions[normalizedEnd - 1]?.sourceEnd ?? 0,
  };
}

export function createDocumentAnchorSelector(
  projection: DocumentTextProjection,
  range: DocumentTextRange,
  options: CreateDocumentAnchorSelectorOptions = {},
): AnnotationAnchorSelector {
  const contextLength = options.contextLength ?? DEFAULT_CONTEXT_LENGTH;
  return {
    quote: {
      exact: range.text,
      prefix: projection.text.slice(Math.max(0, range.normalizedStart - contextLength), range.normalizedStart),
      suffix: projection.text.slice(range.normalizedEnd, range.normalizedEnd + contextLength),
    },
    position: {
      normalizedStart: range.normalizedStart,
      normalizedEnd: range.normalizedEnd,
      markdownStart: range.markdownStart,
      markdownEnd: range.markdownEnd,
    },
  };
}

/** The server's acceptance check (fork: same name). Used by tests to prove a
 * client-built selector would be accepted. */
export function verifyDocumentAnchorSelector(
  input: VerifyDocumentAnchorSelectorInput,
): VerifyDocumentAnchorSelectorResult {
  const projection = projectMarkdownToText(input.markdown);
  const range = resolveProjectionRange(
    projection,
    input.selector.position.normalizedStart,
    input.selector.position.normalizedEnd,
  );
  if (!range) {
    return { ok: false, projection, reason: "invalid_range" };
  }

  if (normalizeAnchorText(range.text) !== normalizeAnchorText(input.selector.quote.exact)) {
    return { ok: false, projection, reason: "quote_mismatch" };
  }

  if (
    range.markdownStart !== input.selector.position.markdownStart
    || range.markdownEnd !== input.selector.position.markdownEnd
  ) {
    return { ok: false, projection, reason: "position_mismatch" };
  }

  return { ok: true, projection, reason: "verified" };
}

function stripBlockSyntax(line: string, absoluteOffset: number): { text: string; sourceOffset: number } {
  const blockMatch = line.match(/^\s{0,3}(?:(#{1,6})\s+|(?:[-+*]|\d+[.)])\s+|>\s?)/);
  if (!blockMatch) return { text: line, sourceOffset: absoluteOffset };
  return { text: line.slice(blockMatch[0].length), sourceOffset: absoluteOffset + blockMatch[0].length };
}

function addInlineMarkdownText(builder: ProjectionBuilder, text: string, sourceOffset: number): void {
  for (let index = 0; index < text.length; index += 1) {
    const char = text[index] ?? "";
    const absolute = sourceOffset + index;
    const rest = text.slice(index);

    const image = rest.match(/^!\[([^\]]*)\]\(([^)]*)\)/);
    if (image) {
      const altStart = absolute + 2;
      builder.addText(image[1] ?? "", altStart);
      index += image[0].length - 1;
      continue;
    }

    const link = rest.match(/^\[([^\]]+)\]\(([^)]*)\)/);
    if (link) {
      const labelStart = absolute + 1;
      builder.addText(link[1] ?? "", labelStart);
      index += link[0].length - 1;
      continue;
    }

    if (char === "`") {
      const closing = text.indexOf("`", index + 1);
      if (closing > index + 1) {
        builder.addText(text.slice(index + 1, closing), absolute + 1);
        index = closing;
        continue;
      }
    }

    if (char === "|" || char === "\t") {
      builder.addSeparator(absolute);
      continue;
    }

    if (isMarkdownFormattingChar(char, text, index)) continue;

    builder.addChar(char, absolute, absolute + 1);
  }
}

function isMarkdownFormattingChar(char: string, text: string, index: number): boolean {
  if (char === "*" || char === "_" || char === "~") return true;
  if (char === "\\" && index + 1 < text.length) return true;
  return false;
}

class ProjectionBuilder {
  private text = "";
  private positions: DocumentTextPosition[] = [];
  private pendingSpace: DocumentTextPosition | null = null;

  constructor(private readonly source: string) {}

  addText(text: string, sourceOffset: number): void {
    for (let index = 0; index < text.length; index += 1) {
      this.addChar(text[index] ?? "", sourceOffset + index, sourceOffset + index + 1);
    }
  }

  addSeparator(sourceOffset: number): void {
    this.addChar(" ", sourceOffset, sourceOffset + 1);
  }

  addChar(char: string, sourceStart: number, sourceEnd: number): void {
    if (/\s/.test(char)) {
      if (this.text.length > 0 && !this.pendingSpace) {
        this.pendingSpace = { sourceStart, sourceEnd };
      }
      return;
    }

    if (this.pendingSpace && this.text.length > 0) {
      this.text += " ";
      this.positions.push(this.pendingSpace);
    }
    this.pendingSpace = null;
    this.text += char;
    this.positions.push({ sourceStart, sourceEnd });
  }

  toProjection(): DocumentTextProjection {
    return {
      source: this.source,
      text: this.text,
      positions: this.positions,
    };
  }
}
