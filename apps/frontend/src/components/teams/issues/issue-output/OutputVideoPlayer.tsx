// Ported from Paperclip ui/src/components/issue-output/OutputVideoPlayer.tsx @
// v2026.916.1-isol8.2 (MIT, (c) 2025 Paperclip AI). `src` is a blob: URL here
// (the bytes come through the Teams BFF), so seeking works without byte ranges.

import { cn } from "@/lib/utils";

interface OutputVideoPlayerProps {
  src: string;
  poster?: string | null;
  className?: string;
  /** Accessible label, typically the filename. */
  title?: string;
}

/**
 * Thin wrapper around the native HTML5 `<video>` element with sensible
 * defaults for issue outputs. We deliberately rely on the browser's native
 * controls (play/pause/scrub/fullscreen/PiP) rather than building a custom
 * scrubber.
 *
 * A fixed 16:9 box reserves height before metadata loads to avoid layout jump.
 */
export function OutputVideoPlayer({ src, poster, className, title }: OutputVideoPlayerProps) {
  return (
    <div
      className={cn("relative aspect-video w-full overflow-hidden rounded-md", className)}
      style={{ background: "var(--midnight)" }}
    >
      <video
        src={src}
        poster={poster ?? undefined}
        controls
        preload="metadata"
        playsInline
        aria-label={title ? `Video output: ${title}` : "Video output"}
        className="absolute inset-0 h-full w-full"
      />
    </div>
  );
}
