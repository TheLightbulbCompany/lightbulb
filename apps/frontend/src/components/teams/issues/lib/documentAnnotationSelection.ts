// Annotation-anchor helpers (A3), adapted from upstream Paperclip
// `ui/src/lib/document-annotation-selection.ts`. The selector a new thread
// carries is built with the fork's own markdown→text projection
// (`./documentAnchors`, ported from `packages/shared/src/document-anchors.ts`),
// because the server verifies it against that projection and 422s on any
// mismatch. The rendered DOM selection only supplies the quote and which
// occurrence the reader picked.

import {
  createDocumentAnchorSelector,
  projectMarkdownToText,
  resolveProjectionRange,
} from "./documentAnchors";
import type { PendingAnchor } from "./documentReviewTypes";

/** Collapse whitespace runs to a single space and trim, mirroring upstream's
 * `normalizeAnchorText` so the same substring matches across renders. */
export function normalizeAnchorText(text: string): string {
  return text.replace(/\s+/g, " ").trim();
}

export interface ContainerTextOffset {
  startOffset: number;
  endOffset: number;
  containerText: string;
  selectedText: string;
}

/** Flatten a DOM selection `Range` into raw character offsets within
 * `container.textContent`. Returns null when the selection escapes the
 * container or is collapsed. */
export function getContainerTextOffset(
  container: HTMLElement,
  range: Range,
): ContainerTextOffset | null {
  if (!container.contains(range.startContainer) || !container.contains(range.endContainer)) {
    return null;
  }
  const preRange = range.startContainer.ownerDocument!.createRange();
  preRange.selectNodeContents(container);
  preRange.setEnd(range.startContainer, range.startOffset);
  const startOffset = preRange.toString().length;
  preRange.setEnd(range.endContainer, range.endOffset);
  const endOffset = preRange.toString().length;
  if (endOffset <= startOffset) return null;
  return {
    startOffset,
    endOffset,
    containerText: container.textContent ?? "",
    selectedText: range.toString(),
  };
}

function findAllOccurrences(haystack: string, needle: string): number[] {
  if (!needle) return [];
  const out: number[] = [];
  let cursor = haystack.indexOf(needle);
  while (cursor !== -1) {
    out.push(cursor);
    cursor = haystack.indexOf(needle, cursor + 1);
  }
  return out;
}

function pickClosest(occurrences: number[], expected: number): number | null {
  if (occurrences.length === 0) return null;
  let best = occurrences[0];
  let bestDistance = Math.abs(best - expected);
  for (const candidate of occurrences) {
    const distance = Math.abs(candidate - expected);
    if (distance < bestDistance) {
      best = candidate;
      bestDistance = distance;
    }
  }
  return best;
}

/**
 * Pure anchor computation — testable without a DOM. Builds a valid
 * `AnnotationAnchorSelector` from a raw text selection inside `containerText`,
 * pinned against the raw `markdown` body. Returns null when the selection is
 * empty or can't be located.
 */
export function computeAnchor(input: {
  markdown: string;
  containerText: string;
  startOffset: number;
  endOffset: number;
}): PendingAnchor | null {
  const rawSelected = input.containerText.slice(input.startOffset, input.endOffset);
  const needle = normalizeAnchorText(rawSelected);
  if (!needle) return null;

  // Which occurrence of the quote the reader selected, in rendered space.
  const normalizedContainer = normalizeAnchorText(input.containerText);
  const rendered = findAllOccurrences(normalizedContainer, needle);
  if (rendered.length === 0) return null;
  const ratio = input.startOffset / Math.max(1, input.containerText.length);
  const renderedStart = pickClosest(rendered, Math.round(ratio * normalizedContainer.length));
  if (renderedStart == null) return null;

  // Find the same quote in the server's projection. The projection drops
  // `*`, `_`, `~` and turns `|` into a space even when they render literally
  // (escaped or mid-word), so retry with the quote in that shape.
  const projection = projectMarkdownToText(input.markdown);
  let quote = needle;
  let candidates = findAllOccurrences(projection.text, quote);
  if (candidates.length === 0) {
    quote = normalizeAnchorText(needle.replace(/[*_~]/g, "").replace(/\|/g, " "));
    candidates = quote ? findAllOccurrences(projection.text, quote) : [];
  }
  if (candidates.length === 0) return null; // the server would refuse it

  // Same occurrence count → take the reader's k-th; otherwise the nearest by
  // relative position.
  const projectionStart =
    candidates.length === rendered.length
      ? candidates[rendered.indexOf(renderedStart)]
      : pickClosest(candidates, Math.round((renderedStart / Math.max(1, normalizedContainer.length)) * projection.text.length));
  if (projectionStart == null) return null;

  const range = resolveProjectionRange(projection, projectionStart, projectionStart + quote.length);
  if (!range) return null;
  return { selector: createDocumentAnchorSelector(projection, range), selectedText: rawSelected };
}

/** Compose {@link getContainerTextOffset} + {@link computeAnchor} for a live
 * DOM selection. */
export function buildAnchorFromSelection(input: {
  markdown: string;
  container: HTMLElement;
  range: Range;
}): PendingAnchor | null {
  const offset = getContainerTextOffset(input.container, input.range);
  if (!offset) return null;
  return computeAnchor({
    markdown: input.markdown,
    containerText: offset.containerText,
    startOffset: offset.startOffset,
    endOffset: offset.endOffset,
  });
}

/**
 * Walk the container's text nodes and return `Range`s covering an occurrence of
 * `exact` (normalized match). When `expectedNormalizedStart` is provided (the
 * thread's stored normalized offset), resolve to the occurrence CLOSEST to it —
 * otherwise a comment on a phrase that REPEATS in the document would always
 * highlight the first match, drawing the wrong span. Falls back to the first
 * occurrence when no stored offset is available. Each Range can be
 * rectangle-projected via `getClientRects()` to draw a highlight overlay.
 */
export function rangesForText(
  container: HTMLElement,
  exact: string,
  expectedNormalizedStart?: number | null,
): Range[] {
  const needle = normalizeAnchorText(exact);
  if (!needle) return [];
  const containerText = container.textContent ?? "";
  const normalizedContainer = normalizeAnchorText(containerText);
  const occurrences = findAllOccurrences(normalizedContainer, needle);
  if (occurrences.length === 0) return [];
  const normIndex =
    expectedNormalizedStart == null
      ? occurrences[0]
      : pickClosest(occurrences, expectedNormalizedStart) ?? occurrences[0];

  const rawStart = mapNormalizedOffsetToRaw(containerText, normIndex);
  if (rawStart < 0) return [];
  const rawLen = matchRawLengthForNormalized(containerText.slice(rawStart), needle.length);
  if (rawLen <= 0) return [];

  return buildRangesForRawSpan(container, rawStart, rawStart + rawLen);
}

function mapNormalizedOffsetToRaw(rawText: string, normalizedOffset: number): number {
  let normalizedCursor = 0;
  let lastWasWhitespace = true; // mimic trim() at start
  for (let index = 0; index < rawText.length; index += 1) {
    const char = rawText[index] ?? "";
    if (/\s/.test(char)) {
      if (!lastWasWhitespace) {
        if (normalizedCursor === normalizedOffset) return index;
        normalizedCursor += 1;
        lastWasWhitespace = true;
      }
      continue;
    }
    if (normalizedCursor === normalizedOffset) return index;
    normalizedCursor += 1;
    lastWasWhitespace = false;
  }
  return -1;
}

function matchRawLengthForNormalized(rawTail: string, normalizedLength: number): number {
  let normalizedCount = 0;
  let lastWasWhitespace = false;
  for (let index = 0; index < rawTail.length; index += 1) {
    const char = rawTail[index] ?? "";
    if (/\s/.test(char)) {
      if (!lastWasWhitespace) {
        normalizedCount += 1;
        if (normalizedCount >= normalizedLength) return index;
        lastWasWhitespace = true;
      }
    } else {
      normalizedCount += 1;
      lastWasWhitespace = false;
      if (normalizedCount >= normalizedLength) return index + 1;
    }
  }
  return rawTail.length;
}

function buildRangesForRawSpan(container: HTMLElement, rawStart: number, rawEnd: number): Range[] {
  const doc = container.ownerDocument!;
  const walker = doc.createTreeWalker(container, NodeFilter.SHOW_TEXT, null);
  const ranges: Range[] = [];
  let cursor = 0;
  let node: Node | null = walker.nextNode();
  while (node) {
    const textNode = node as Text;
    const length = textNode.data.length;
    const nodeStart = cursor;
    const nodeEnd = cursor + length;
    if (nodeEnd > rawStart && nodeStart < rawEnd) {
      const startWithin = Math.max(0, rawStart - nodeStart);
      const endWithin = Math.min(length, rawEnd - nodeStart);
      if (endWithin > startWithin) {
        const range = doc.createRange();
        range.setStart(textNode, startWithin);
        range.setEnd(textNode, endWithin);
        ranges.push(range);
      }
    }
    cursor = nodeEnd;
    if (cursor >= rawEnd) break;
    node = walker.nextNode();
  }
  return ranges;
}
