// Ported from Paperclip ui/src/components/issue-output/OutputFileTile.tsx @
// v2026.916.1-isol8.2 (MIT, (c) 2025 Paperclip AI). Tone classes map onto the
// brand state tokens instead of Tailwind palette colours.

import type { CSSProperties } from "react";
import { cn } from "@/lib/utils";
import { getOutputFileGlyph, type OutputFileTone } from "../lib/issue-output";

const TONE_VARS: Record<OutputFileTone, string> = {
  video: "var(--info)",
  pdf: "var(--err)",
  zip: "var(--warn)",
  image: "var(--ok)",
  bin: "var(--midnight-muted)",
};

interface OutputFileTileProps {
  contentType: string | null | undefined;
  className?: string;
  /** Tailwind size classes for the square tile. Defaults to a 32×32 tile. */
  sizeClassName?: string;
}

/** Square file-type tile showing a short MIME-derived label, colorised by tone. */
export function OutputFileTile({ contentType, className, sizeClassName = "h-8 w-8 text-2xs" }: OutputFileTileProps) {
  const glyph = getOutputFileGlyph(contentType);
  const tone = TONE_VARS[glyph.tone];
  const style: CSSProperties = {
    color: tone,
    background: `color-mix(in srgb, ${tone} 15%, transparent)`,
  };
  return (
    <span
      className={cn(
        "flex shrink-0 items-center justify-center rounded-md font-semibold tabular-nums",
        sizeClassName,
        className,
      )}
      style={style}
      aria-hidden="true"
    >
      {glyph.label}
    </span>
  );
}
