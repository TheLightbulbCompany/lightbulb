// apps/frontend/src/components/teams/cases/CaseAttachmentsGallery.tsx

// Ported from upstream Paperclip's CaseAttachmentsGallery (paperclip/ui/src/components/CaseAttachmentsGallery.tsx)
// (MIT, (c) 2025 Paperclip AI).
//
// Upstream points <img> straight at `/api/assets/{id}/content` (same origin,
// cookie auth) and opens a shared lightbox. Here the bytes only come through
// the Teams BFF with a Clerk bearer, case-scoped, so each thumbnail is fetched
// as a Blob; Teams has no lightbox, so an image opens in a new tab instead.

"use client";

import { useEffect, useRef, useState } from "react";
import { FileText } from "lucide-react";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import type { CaseAttachmentRef } from "@/components/teams/shared/types";
import { cn } from "@/lib/utils";

// Raster types only: a blob: URL carries our origin, so an SVG must never be
// opened as a document.
const IMAGE_TYPE = /^image\/(png|jpe?g|gif|webp|avif)$/;

function isImageAttachment(attachment: CaseAttachmentRef): boolean {
  return IMAGE_TYPE.test(attachment.asset.contentType.toLowerCase().split(";")[0].trim());
}

function humanBytes(size: number): string {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(0)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

function AttachmentImage({ path, alt }: { path: string; alt: string }) {
  const { download } = useTeamsApi();
  // `download` is a fresh function every render; capture it once so the fetch
  // depends only on the path (same mount-ref as useAttachmentObjectUrl).
  const downloadRef = useRef(download);
  const [url, setUrl] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    let objectUrl: string | undefined;
    downloadRef
      .current(path)
      .then((blob) => {
        if (cancelled) return;
        objectUrl = URL.createObjectURL(blob);
        setUrl(objectUrl);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [path]);

  if (!url) return <div className="h-full w-full animate-pulse bg-[var(--plane-raised)]" />;
  return (
    <a href={url} target="_blank" rel="noreferrer" className="block h-full w-full">
      {/* eslint-disable-next-line @next/next/no-img-element -- a blob: URL; next/image cannot load it */}
      <img
        src={url}
        alt={alt}
        loading="lazy"
        className="h-full w-full object-cover transition-transform group-hover:scale-[1.02]"
      />
    </a>
  );
}

/**
 * Attachments gallery: image-friendly grid. Image cases hold variations as
 * attachments, so images render as thumbnails; non-image assets fall back to a
 * labelled file tile. No variation-picker (out of scope).
 */
export function CaseAttachmentsGallery({
  caseId,
  attachments,
}: {
  caseId: string;
  attachments: CaseAttachmentRef[];
}) {
  if (attachments.length === 0) {
    return <p className="text-xs text-[var(--fg-mute)]">No attachments.</p>;
  }

  return (
    <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 md:grid-cols-4">
      {attachments.map((attachment) => {
        const isImage = isImageAttachment(attachment);
        const filename = attachment.asset.originalFilename ?? "attachment";
        return (
          <div
            key={attachment.id}
            title={filename}
            className={cn(
              "group relative flex aspect-square flex-col overflow-hidden rounded-lg border border-[var(--edge)] bg-[var(--plane-sidebar)] text-left",
              isImage && "hover:border-[var(--fg-mute)]",
            )}
          >
            {isImage ? (
              <AttachmentImage
                path={`/cases/${caseId}/attachments/${attachment.id}/content`}
                alt={filename}
              />
            ) : (
              <div className="flex h-full w-full flex-col items-center justify-center gap-1 p-2 text-[var(--fg-mute)]">
                <FileText className="h-6 w-6" aria-hidden />
                <span className="w-full truncate text-center text-2xs">{filename}</span>
              </div>
            )}
            <span className="pointer-events-none absolute inset-x-0 bottom-0 truncate bg-[var(--plane-pane)]/80 px-1.5 py-1 text-2xs text-[var(--fg-soft)]">
              {humanBytes(attachment.asset.byteSize)}
            </span>
          </div>
        );
      })}
    </div>
  );
}
