// apps/frontend/src/components/teams/search/HighlightedText.tsx
//
// Ported from upstream Paperclip (ui/src/components/search/HighlightedText.tsx,
// MIT © 2025 Paperclip AI). Renders text with server-provided match ranges
// wrapped in <mark>. Ranges are clamped to the text bounds and merged so
// overlapping/adjacent highlights don't produce nested marks. Styling adapted
// to Isol8's editorial tokens.

import { cn } from "@/lib/utils";
import type { CompanySearchHighlight } from "@/components/teams/shared/types";

export interface HighlightedTextProps {
  text: string;
  highlights?: readonly CompanySearchHighlight[] | null;
  className?: string;
}

function clampedRanges(
  text: string,
  highlights: readonly CompanySearchHighlight[],
): Array<{ start: number; end: number }> {
  const result: Array<{ start: number; end: number }> = [];
  for (const range of highlights) {
    const start = Math.max(0, Math.min(text.length, range.start));
    const end = Math.max(start, Math.min(text.length, range.end));
    if (end <= start) continue;
    result.push({ start, end });
  }
  result.sort((a, b) => a.start - b.start);
  const merged: Array<{ start: number; end: number }> = [];
  for (const range of result) {
    const last = merged[merged.length - 1];
    if (last && range.start <= last.end) {
      last.end = Math.max(last.end, range.end);
    } else {
      merged.push({ ...range });
    }
  }
  return merged;
}

export function HighlightedText({ text, highlights, className }: HighlightedTextProps) {
  const ranges = highlights && highlights.length > 0 ? clampedRanges(text, highlights) : [];
  if (ranges.length === 0) {
    return <span className={className}>{text}</span>;
  }
  const segments: Array<{ key: string; text: string; highlight: boolean }> = [];
  let cursor = 0;
  ranges.forEach((range, index) => {
    if (range.start > cursor) {
      segments.push({ key: `t-${index}`, text: text.slice(cursor, range.start), highlight: false });
    }
    segments.push({ key: `m-${index}`, text: text.slice(range.start, range.end), highlight: true });
    cursor = range.end;
  });
  if (cursor < text.length) {
    segments.push({ key: "t-end", text: text.slice(cursor), highlight: false });
  }
  return (
    <span className={className}>
      {segments.map((segment) =>
        segment.highlight ? (
          <mark
            key={segment.key}
            className={cn("rounded-sm px-0.5")}
            style={{ background: "var(--bulb)", color: "var(--midnight)" }}
          >
            {segment.text}
          </mark>
        ) : (
          <span key={segment.key}>{segment.text}</span>
        ),
      )}
    </span>
  );
}
