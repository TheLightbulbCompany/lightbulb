// Ported from Paperclip ui/src/components/issue-output/OutputRow.tsx @
// v2026.916.1-isol8.2 (MIT, (c) 2025 Paperclip AI). Open/Download fetch through
// the Teams BFF (./attachmentBlob); shadcn Card/Button become brand-token markup.

"use client";

import { Download, ExternalLink } from "lucide-react";
import { timeAgo } from "@/components/teams/shared/lib/timeAgo";
import { formatBytes, outputFilename, type IssueOutputItem } from "../lib/issue-output";
import { OutputFileTile } from "./OutputFileTile";
import { useAttachmentActions } from "./attachmentBlob";

interface OutputRowProps {
  item: IssueOutputItem;
  creatorName?: string | null;
}

const ICON_BUTTON =
  "inline-flex h-7 w-7 items-center justify-center rounded-sm transition-colors hover:bg-[var(--cream-deep)]";

/** Compact row for a non-primary output ("ALSO PRODUCED"). */
export function OutputRow({ item, creatorName }: OutputRowProps) {
  const filename = outputFilename(item);
  const meta = item.metadata;
  const actions = useAttachmentActions();

  const metaBits: string[] = [];
  if (meta) {
    metaBits.push(meta.contentType);
    metaBits.push(formatBytes(meta.byteSize));
  }
  if (creatorName) metaBits.push(creatorName);
  metaBits.push(timeAgo(new Date(item.createdAt)));

  return (
    <div className="flex items-center gap-2.5 rounded-md p-2" style={{ border: "1px solid var(--edge)" }}>
      <OutputFileTile contentType={meta?.contentType} />
      <div className="min-w-0 flex-1">
        <p className="truncate text-ui font-medium" style={{ color: "var(--midnight)" }} title={filename}>
          {filename}
        </p>
        <p className="truncate text-caption" style={{ color: item.degraded ? "var(--err)" : "var(--midnight-muted)" }}>
          {item.degraded ? "File details unavailable" : metaBits.join(" · ")}
        </p>
      </div>
      {meta ? (
        <div className="flex shrink-0 items-center gap-1" style={{ color: "var(--midnight-muted)" }}>
          <button
            type="button"
            className={ICON_BUTTON}
            title="Open in new tab"
            aria-label={`Open ${filename}`}
            onClick={() => void actions.open(meta.openPath, filename, meta.contentType)}
          >
            <ExternalLink size={16} />
          </button>
          <button
            type="button"
            className={ICON_BUTTON}
            title="Download"
            aria-label={`Download ${filename}`}
            onClick={() => void actions.download(meta.downloadPath, filename)}
          >
            <Download size={16} />
          </button>
        </div>
      ) : null}
    </div>
  );
}
