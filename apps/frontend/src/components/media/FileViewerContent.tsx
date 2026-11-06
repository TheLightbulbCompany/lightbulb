"use client";

import { cn, formatBytes } from "@/lib/utils";
import { DOCX, DocxPreview } from "./DocxPreview";
import { HtmlPreview } from "./HtmlPreview";
import { SheetGrid } from "./SheetGrid";
import { TextPreview } from "./TextPreview";

const XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet";
const PANE_VIEWABLE = new Set(["application/pdf", XLSX, DOCX, "text/csv", "text/html", "text/markdown", "text/plain"]);
/** Kinds that open in the right pane (documents). Images and video keep the lightbox; zip/pptx have no viewer. */
export function isPaneViewable(contentType: string): boolean { return PANE_VIEWABLE.has(normalizeContentType(contentType)); }
/** "Text/Markdown; charset=utf-8" -> "text/markdown"; the viewer matches types exactly. */
export function normalizeContentType(contentType: string | null | undefined): string {
  return (contentType ?? "").split(";")[0].trim().toLowerCase();
}

export interface FileViewerItem {
  fileName: string;
  contentType: string;
  size?: number;
  /** Object or HTTP URL for the file's bytes. */
  url: string;
  /** A video's poster frame (an image sent in the same message). */
  poster?: string;
  /** The bytes, when the caller has them; the spreadsheet, HTML and text branches need them. */
  blob?: Blob;
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
 *   video/*          -> the browser's native <video> player (for a room
 *                        attachment the url is a presigned S3 URL, not a blob).
 *   anything else     -> filename + size card; the caller supplies its own
 *                        Download control.
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

  if (item.contentType.startsWith("video/")) {
    return <video src={item.url} poster={item.poster} controls playsInline preload="metadata" className={cn("max-h-full max-w-full", className)} />;
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

  if (item.blob && (item.contentType === XLSX || item.contentType === "text/csv")) return <SheetGrid blob={item.blob} fileName={item.fileName} csv={item.contentType === "text/csv"} />;
  if (item.blob && item.contentType === DOCX) return <DocxPreview blob={item.blob} fileName={item.fileName} />;
  if (item.blob && item.contentType === "text/html") return <HtmlPreview blob={item.blob} fileName={item.fileName} />;
  if (item.blob && (item.contentType === "text/markdown" || item.contentType === "text/plain")) return <TextPreview blob={item.blob} fileName={item.fileName} markdown={item.contentType === "text/markdown"} />;

  return (
    <div className={cn("flex flex-col items-center gap-2 p-8 text-center", className)}>
      <p className="max-w-full truncate text-sm font-medium text-midnight">{item.fileName}</p>
      {item.size != null && <p className="text-xs text-midnight-muted">{formatBytes(item.size)}</p>}
      <p className="text-xs text-midnight-muted">Preview not available for this file type.</p>
    </div>
  );
}
