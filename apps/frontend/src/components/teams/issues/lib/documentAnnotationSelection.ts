// Self-contained annotation-anchor helpers (A3), adapted from upstream
// Paperclip `ui/src/lib/document-annotation-selection.ts` but WITHOUT the
// `@paperclipai/shared` markdown-projection dependency (Isol8 does not vendor
// that package). The anchor a thread carries is a text-quote selector
// (exact + surrounding context) plus a character-position selector; the server
// tolerates approximate positions and re-anchors primarily by quote, so we
// compute positions from the rendered container text + the raw markdown body.

import type { AnnotationAnchorSelector, PendingAnchor } from "./documentReviewTypes";

const CONTEXT_CHARS = 32;

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
 * Locate `needle` inside `target` (the raw or normalized markdown body) at the
 * occurrence that best corresponds to `normalizedOffset` — a position in
 * normalized-container space. When the phrase REPEATS we must not blindly take
 * the first match: `normalizedOffset` was already disambiguated to the user's
 * actual selection, so we scale it into `target`'s coordinate space and pick the
 * closest occurrence. This mirrors how the read path ({@link rangesForText})
 * resolves a repeated span against its own stored offset, so write and read
 * agree on which occurrence a thread anchors to. Returns -1 when absent; a
 * single occurrence resolves exactly like `target.indexOf(needle)`.
 */
function occurrenceClosestToNormalized(
  target: string,
  needle: string,
  normalizedOffset: number,
  normalizedContainerLength: number,
): number {
  const occurrences = findAllOccurrences(target, needle);
  if (occurrences.length === 0) return -1;
  const expected =
    normalizedContainerLength <= 0
      ? normalizedOffset
      : Math.round((normalizedOffset / normalizedContainerLength) * target.length);
  return pickClosest(occurrences, expected) ?? occurrences[0];
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

  const normalizedContainer = normalizeAnchorText(input.containerText);
  const occurrences = findAllOccurrences(normalizedContainer, needle);
  if (occurrences.length === 0) return null;

  const ratio = input.startOffset / Math.max(1, input.containerText.length);
  const expected = Math.round(ratio * normalizedContainer.length);
  const normalizedStart = pickClosest(occurrences, expected);
  if (normalizedStart == null) return null;
  const normalizedEnd = normalizedStart + needle.length;

  // Prefix/suffix context from the raw container text (bounded so it never
  // trips the upstream 1000-char cap).
  const prefix = input.containerText.slice(Math.max(0, input.startOffset - CONTEXT_CHARS), input.startOffset);
  const suffix = input.containerText.slice(input.endOffset, input.endOffset + CONTEXT_CHARS);

  // Position into the raw markdown body: prefer an exact substring match, then
  // a normalized match, else fall back to the normalized-container offsets so
  // the (end > start) invariant always holds. When the selected text REPEATS in
  // the document, resolve to the occurrence closest to the user's actual
  // selection (`normalizedStart`) rather than the first — otherwise the selector
  // would describe an earlier duplicate and the backend could re-anchor the new
  // thread onto the wrong span.
  let markdownStart = normalizedStart;
  let markdownEnd = normalizedEnd;
  const rawIdx = occurrenceClosestToNormalized(
    input.markdown,
    rawSelected,
    normalizedStart,
    normalizedContainer.length,
  );
  if (rawIdx !== -1) {
    markdownStart = rawIdx;
    markdownEnd = rawIdx + rawSelected.length;
  } else {
    const normalizedMarkdown = normalizeAnchorText(input.markdown);
    const normIdx = occurrenceClosestToNormalized(
      normalizedMarkdown,
      needle,
      normalizedStart,
      normalizedContainer.length,
    );
    if (normIdx !== -1) {
      markdownStart = normIdx;
      markdownEnd = normIdx + needle.length;
    }
  }

  const selector: AnnotationAnchorSelector = {
    quote: {
      exact: rawSelected.slice(0, 10000),
      prefix: prefix.slice(-1000),
      suffix: suffix.slice(0, 1000),
    },
    position: { normalizedStart, normalizedEnd, markdownStart, markdownEnd },
  };
  return { selector, selectedText: rawSelected };
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
