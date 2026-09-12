"use client";

import { cn, formatBytes } from "@/lib/utils";

export interface FileViewerItem {
  fileName: string;
  contentType: string;
  size?: number;
  /** Object or HTTP URL for the file's bytes. */
  url: string;
}

/**
 * Content-type-driven file preview, shared by the chat-attachment viewer
 * dialog (FileViewerDialog) and the workspace file pane (FileContentViewer's
 * PDF branch) — the one place a new content type gets handled, instead of
 * two independent "how do I show this" implementations.
 *
 *   image/*          -> the image, fit to its container.
 *   application/pdf  -> the browser's own PDF renderer (Chrome and WKWebView,
 *                        which is what the Tauri desktop app runs, both render
 *                        PDFs natively via <iframe> — no pdf.js dependency).
 *   anything else     -> filename + size card. No player — video included,
 *                        per the product decision to skip a video player for
 *                        now; the caller supplies its own Download control.
 */
export function FileViewerContent({ item, className }: { item: FileViewerItem; className?: string }) {
  if (item.contentType.startsWith("image/")) {
    return (
      // eslint-disable-next-line @next/next/no-img-element -- blob/object URL, not a Next-optimizable asset
      <img
        src={item.url}
        alt={item.fileName}
        className={cn("max-h-full max-w-full object-contain", className)}
      />
    );
  }

  if (item.contentType === "application/pdf") {
    return (
      <iframe
        src={item.url}
        title={item.fileName}
        className={cn("h-full w-full border-0 bg-cream", className)}
      />
    );
  }

  return (
    <div className={cn("flex flex-col items-center gap-2 p-8 text-center", className)}>
      <p className="max-w-full truncate text-sm font-medium text-midnight">{item.fileName}</p>
      {item.size != null && <p className="text-xs text-midnight-muted">{formatBytes(item.size)}</p>}
      <p className="text-xs text-midnight-muted">Preview not available for this file type.</p>
    </div>
  );
}
