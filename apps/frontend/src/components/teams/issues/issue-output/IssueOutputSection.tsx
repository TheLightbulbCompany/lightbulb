// Ported from Paperclip ui/src/components/issue-output/IssueOutputSection.tsx @
// v2026.916.1-isol8.2 (MIT, (c) 2025 Paperclip AI). Adaptations: preview media
// loads as blobs through the Teams BFF (./attachmentBlob); the gallery
// `onMediaClick` path is dropped (tiles open the file); the section header uses
// the side panel's `section-label` style.

"use client";

/* eslint-disable @next/next/no-img-element -- blob: object URLs, not optimizable remote assets */

import type { IssueWorkProduct } from "@/components/teams/shared/types";
import { timeAgo } from "@/components/teams/shared/lib/timeAgo";
import {
  formatBytes,
  getIssueOutputs,
  isImageContentType,
  isVideoLikeOutput,
  outputFilename,
  type IssueOutputItem,
} from "../lib/issue-output";
import { OutputPrimaryCard } from "./OutputPrimaryCard";
import { OutputRow } from "./OutputRow";
import { OutputFileTile } from "./OutputFileTile";
import { useAttachmentActions, useAttachmentObjectUrl } from "./attachmentBlob";

interface IssueOutputSectionProps {
  workProducts: IssueWorkProduct[] | null | undefined;
  /** Optional resolver for the artifact creator's display name. */
  resolveCreatorName?: (item: IssueOutputItem) => string | null;
}

function isMediaOutput(item: IssueOutputItem) {
  const meta = item.metadata;
  return Boolean(meta && (
    isImageContentType(meta.contentType) ||
    isVideoLikeOutput(meta.contentType, meta.originalFilename)
  ));
}

function OutputMediaPreview({
  item,
  creatorName,
}: {
  item: IssueOutputItem;
  creatorName?: string | null;
}) {
  const meta = item.metadata;
  const url = useAttachmentObjectUrl(meta?.contentPath);
  const actions = useAttachmentActions();
  if (!meta) return null;

  const filename = outputFilename(item);
  const isVideo = isVideoLikeOutput(meta.contentType, meta.originalFilename);
  const metaBits = [meta.contentType, formatBytes(meta.byteSize)];
  if (creatorName) metaBits.push(creatorName);
  metaBits.push(timeAgo(new Date(item.createdAt)));

  return (
    <button
      type="button"
      className="group relative block aspect-square w-full overflow-hidden rounded-md"
      style={{ border: "1px solid var(--edge)", background: "var(--cream-deep)" }}
      aria-label={`Open ${filename}`}
      onClick={() => void actions.open(meta.openPath, filename, meta.contentType)}
    >
      {!url ? (
        <span className="flex h-full w-full items-center justify-center">
          <OutputFileTile contentType={meta.contentType} />
        </span>
      ) : isVideo ? (
        <video src={url} className="h-full w-full object-cover" muted playsInline preload="metadata" />
      ) : (
        <img src={url} alt={filename} className="h-full w-full object-cover" loading="lazy" />
      )}
      <div
        className="absolute inset-x-0 bottom-0 px-2 py-1.5 text-left"
        style={{ background: "color-mix(in srgb, var(--midnight) 65%, transparent)" }}
      >
        <p className="truncate text-caption font-medium" style={{ color: "var(--cream)" }} title={filename}>{filename}</p>
        <p className="truncate text-2xs" style={{ color: "var(--cream-deep)" }}>{metaBits.join(" · ")}</p>
      </div>
    </button>
  );
}

/**
 * Issue Output surface (PAP-10162 Phase 3).
 *
 * Renders attachment-backed artifact work products as first-class issue
 * outputs: a full-width primary card (video player / image / generic file) with
 * Open + Download, plus compact rows for any additional outputs. The section is
 * omitted entirely when the issue has produced no outputs — we never show a
 * permanent empty card.
 */
export function IssueOutputSection({ workProducts, resolveCreatorName }: IssueOutputSectionProps) {
  const { primary, rest, count } = getIssueOutputs(workProducts);

  if (!primary) return null;

  const creatorFor = (item: IssueOutputItem) => resolveCreatorName?.(item) ?? null;
  const mediaRest = rest.filter(isMediaOutput);
  const fileRest = rest.filter((item) => !isMediaOutput(item));

  return (
    <section className="space-y-3" aria-label="Task outputs">
      <div className="flex items-center gap-2">
        <h2 className="section-label">
          <b>Output</b>
        </h2>
        <span className="text-caption" style={{ color: "var(--midnight-muted)" }}>{count}</span>
      </div>

      {/* Stable anchor target so company Artifacts cards can deep-link to a
          specific work product inside its issue context (PAP-10359). */}
      <div id={`work-product-${primary.id}`} className="scroll-mt-20">
        <OutputPrimaryCard item={primary} creatorName={creatorFor(primary)} />
      </div>

      {rest.length > 0 ? (
        <div className="space-y-2">
          <p className="label-caps" style={{ color: "var(--midnight-muted)" }}>Also produced</p>
          {mediaRest.length > 0 ? (
            <div className="grid grid-cols-4 gap-2">
              {mediaRest.map((item) => (
                <div key={item.id} id={`work-product-${item.id}`} className="scroll-mt-20">
                  <OutputMediaPreview item={item} creatorName={creatorFor(item)} />
                </div>
              ))}
            </div>
          ) : null}
          {fileRest.map((item) => (
            <div key={item.id} id={`work-product-${item.id}`} className="scroll-mt-20">
              <OutputRow item={item} creatorName={creatorFor(item)} />
            </div>
          ))}
        </div>
      ) : null}
    </section>
  );
}
