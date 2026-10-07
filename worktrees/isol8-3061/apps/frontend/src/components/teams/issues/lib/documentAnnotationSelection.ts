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
 * Mark every raw-markdown character that lives inside an inline link/image
 * DESTINATION — the `(…)` that follows a `](` label close — as non-selectable.
 * The label text between `[` and `]` renders and stays selectable; only the
 * destination (URL + optional title, parens included) is hidden. Balanced
 * parens and backslash escapes inside the destination are tracked so a URL like
 * `(a(b)c)` masks fully. The projection walk consults this mask so a rendered
 * character can never align onto a character buried in a URL.
 */
function linkDestinationMask(markdown: string): Uint8Array {
  const hidden = new Uint8Array(markdown.length);
  for (let i = 0; i < markdown.length; i += 1) {
    // A destination opens at "](" — the `]` closing a link/image label, then `(`.
    if (markdown[i] !== "]" || markdown[i + 1] !== "(") continue;
    const open = i + 1;
    let depth = 0;
    let j = open;
    for (; j < markdown.length; j += 1) {
      const ch = markdown[j];
      if (ch === "\\") {
        j += 1; // skip the escaped char
        continue;
      }
      if (ch === "(") depth += 1;
      else if (ch === ")") {
        depth -= 1;
        if (depth === 0) break;
      }
    }
    if (j >= markdown.length) continue; // unbalanced — leave it selectable
    for (let k = open; k <= j; k += 1) hidden[k] = 1;
    i = j; // resume scanning after the destination
  }
  return hidden;
}

/**
 * Project a selected span from normalized-container (rendered) space into raw
 * markdown offsets by greedily aligning the rendered text as an in-order
 * SUBSEQUENCE of the raw body. Rendered text is the markdown with its inline
 * syntax removed — link labels keep their visible text, but URLs / link targets
 * / emphasis markers are dropped — so, to a good approximation, every rendered
 * character reappears in the raw markdown in order with the hidden-syntax
 * characters interleaved. Walking that subsequence steps OVER the hidden-syntax
 * characters, so the returned offsets can only land on text the user could
 * actually have selected: selecting the visible word `here` in
 * `[click](https://here.example) here` anchors to the trailing rendered `here`,
 * never the `here` buried inside the URL (which the old raw `indexOf` scan could
 * pick when the scaled offset happened to fall closer to it).
 *
 * Skipping non-matching syntax alone isn't enough when a rendered word ABUTS a
 * link with no separating whitespace (`[x](https://word.example)word`): the walk
 * would keep matching the rendered `word` THROUGH the `word` inside the URL. So
 * link/image destinations are masked ({@link linkDestinationMask}) and treated
 * as non-selectable — the walk never aligns a rendered char inside a URL.
 *
 * Consuming the rendered PREFIX before the needle also advances the raw cursor
 * past every earlier occurrence, so a REPEATED phrase resolves to the same
 * occurrence the read/highlight path draws (which disambiguates by
 * `normalizedStart`) — write and read stay in agreement. Returns null when the
 * rendered text cannot be aligned into the raw body (e.g. the renderer injected
 * text absent from the source), in which case the caller keeps the
 * normalized-container offsets.
 */
function projectRenderedSpanToMarkdown(
  markdown: string,
  normalizedContainer: string,
  normalizedStart: number,
  normalizedEnd: number,
): { start: number; end: number } | null {
  const isSpace = (ch: string) => /\s/.test(ch);
  const hidden = linkDestinationMask(markdown);
  // A raw position is a candidate only when it renders — i.e. not inside a
  // masked link/image destination.
  const isHidden = (pos: number) => hidden[pos] === 1;
  let cursor = 0;
  let start = -1;
  let lastCharStart = -1; // raw offset of the char at normalizedEnd - 1
  for (let index = 0; index < normalizedEnd; index += 1) {
    const target = normalizedContainer[index] ?? "";
    let matched: number;
    if (target === " ") {
      // A normalized space maps to the first whitespace char of the next raw
      // whitespace run; the run is then consumed so block syntax (blank lines,
      // heading markers) between words doesn't derail the alignment. Masked
      // (link-destination) whitespace is skipped, never matched.
      while (cursor < markdown.length && (isHidden(cursor) || !isSpace(markdown[cursor] ?? "")))
        cursor += 1;
      if (cursor >= markdown.length) return null;
      matched = cursor;
      while (cursor < markdown.length && !isHidden(cursor) && isSpace(markdown[cursor] ?? ""))
        cursor += 1;
    } else {
      while (cursor < markdown.length && (isHidden(cursor) || markdown[cursor] !== target))
        cursor += 1;
      if (cursor >= markdown.length) return null;
      matched = cursor;
      cursor += 1;
    }
    if (index === normalizedStart) start = matched;
    if (index === normalizedEnd - 1) lastCharStart = matched;
  }
  if (start < 0 || lastCharStart < start) return null;
  return { start, end: lastCharStart + 1 };
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

  // Position into the raw markdown body. Anchor by projecting the RENDERED span
  // (normalized-container space) into the raw markdown as a subsequence, so we
  // only ever pin to characters that actually render — never to hidden syntax
  // like a URL or link target that happens to contain the selected word. The
  // projection also disambiguates a REPEATED phrase to the user's actual
  // occurrence (it consumes the rendered prefix first), matching how the read
  // path resolves spans. Fall back to the normalized-container offsets (which
  // are rendered-derived and keep end > start) when the rendered text can't be
  // aligned into the raw body.
  let markdownStart = normalizedStart;
  let markdownEnd = normalizedEnd;
  const projected = projectRenderedSpanToMarkdown(
    input.markdown,
    normalizedContainer,
    normalizedStart,
    normalizedEnd,
  );
  if (projected) {
    markdownStart = projected.start;
    markdownEnd = projected.end;
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
