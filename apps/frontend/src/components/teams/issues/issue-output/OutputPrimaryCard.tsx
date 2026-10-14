// Ported from Paperclip ui/src/components/issue-output/OutputPrimaryCard.tsx @
// v2026.916.1-isol8.2 (MIT, (c) 2025 Paperclip AI). Adaptations: media src and
// Open/Download go through the Teams BFF as blobs (./attachmentBlob) instead of
// same-origin hrefs; the gallery "Browse" action is dropped (no issue gallery
// here); shadcn Card/Button/Badge become brand-token markup.

"use client";

/* eslint-disable @next/next/no-img-element -- blob: object URLs, not optimizable remote assets */

import { Download, ExternalLink } from "lucide-react";
import { timeAgo } from "@/components/teams/shared/lib/timeAgo";
import {
  formatBytes,
  isImageContentType,
  isVideoLikeOutput,
  outputFilename,
  type IssueOutputItem,
} from "../lib/issue-output";
import { OutputVideoPlayer } from "./OutputVideoPlayer";
import { OutputFileTile } from "./OutputFileTile";
import { useAttachmentActions, useAttachmentObjectUrl } from "./attachmentBlob";

interface OutputPrimaryCardProps {
  item: IssueOutputItem;
  creatorName?: string | null;
}

export const OUTPUT_ACTION_CLASS =
  "inline-flex items-center justify-center gap-1.5 rounded-sm px-2.5 py-1 text-caption font-medium transition-colors hover:bg-[var(--cream-deep)] max-md:flex-1";

/**
 * Full-width primary output card: media region (video / image / generic file)
 * over a metadata strip with Open + Download actions. The layout stacks on
 * mobile and uses a single horizontal meta row on desktop.
 */
export function OutputPrimaryCard({ item, creatorName }: OutputPrimaryCardProps) {
  const meta = item.metadata;
  const filename = outputFilename(item);
  const contentType = meta?.contentType;
  const isImage = Boolean(meta && isImageContentType(contentType));
  const isVideo = Boolean(meta && isVideoLikeOutput(contentType, meta.originalFilename));
  const mediaUrl = useAttachmentObjectUrl(isImage || isVideo ? meta?.contentPath : null);
  const actions = useAttachmentActions();

  return (
    <div className="block overflow-hidden rounded-md" style={{ border: "1px solid var(--edge)" }}>
      {/* Media region */}
      {isVideo && mediaUrl ? (
        <OutputVideoPlayer src={mediaUrl} title={filename} className="rounded-none" />
      ) : isImage && mediaUrl ? (
        <button
          type="button"
          className="block aspect-video w-full overflow-hidden"
          style={{ background: "var(--midnight)" }}
          aria-label={`Open ${filename}`}
          onClick={() => meta && void actions.open(meta.openPath, filename, contentType)}
        >
          <img src={mediaUrl} alt={filename} className="h-full w-full object-contain" />
        </button>
      ) : (
        <div className="flex aspect-video w-full items-center justify-center" style={{ background: "var(--cream-deep)" }}>
          <OutputFileTile contentType={contentType} sizeClassName="h-16 w-16 text-base" />
        </div>
      )}

      {/* Metadata strip */}
      <div className="flex flex-col gap-2 p-3 md:flex-row md:items-center md:justify-between">
        <div className="min-w-0 flex-1">
          <p className="break-words text-ui font-semibold" style={{ color: "var(--midnight)" }}>{filename}</p>
          {item.degraded ? (
            <p className="mt-0.5 text-caption" style={{ color: "var(--err)" }}>
              Output metadata is unavailable — this file can’t be played or downloaded here.
            </p>
          ) : (
            <div className="mt-0.5 flex flex-wrap items-center gap-x-1.5 gap-y-0.5 text-caption" style={{ color: "var(--midnight-muted)" }}>
              {item.isPrimary && (
                <span className="label-caps rounded-sm px-1.5 py-0" style={{ border: "1px solid var(--edge)", color: "var(--warn)" }}>
                  Primary
                </span>
              )}
              {meta && <span>{meta.contentType}</span>}
              {meta && <span aria-hidden="true">·</span>}
              {meta && <span>{formatBytes(meta.byteSize)}</span>}
              {creatorName && <span aria-hidden="true">·</span>}
              {creatorName && <span>{creatorName}</span>}
              <span aria-hidden="true">·</span>
              <span>{timeAgo(new Date(item.createdAt))}</span>
            </div>
          )}
        </div>

        {meta ? (
          <div className="flex shrink-0 items-center gap-2 max-md:w-full">
            <button
              type="button"
              className={OUTPUT_ACTION_CLASS}
              style={{ border: "1px solid var(--edge)", color: "var(--midnight)" }}
              onClick={() => void actions.open(meta.openPath, filename, contentType)}
            >
              <ExternalLink size={16} />
              Open
            </button>
            <button
              type="button"
              className={OUTPUT_ACTION_CLASS}
              style={{ background: "var(--midnight)", color: "var(--cream)" }}
              aria-label={`Download ${filename}`}
              onClick={() => void actions.download(meta.downloadPath, filename)}
            >
              <Download size={16} />
              Download
            </button>
          </div>
        ) : null}
      </div>
    </div>
  );
}
