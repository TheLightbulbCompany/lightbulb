"use client";

import { useRef, useState } from "react";
import { Download, Paperclip, Trash2, Upload } from "lucide-react";
import { timeAgo } from "@/components/teams/shared/lib/timeAgo";
import { useIssueAttachments } from "./hooks/useIssueAttachments";
import type { IssueAttachment } from "@/components/teams/shared/types";

/** Human-readable byte size. Local (no shared helper exists in the teams tree). */
function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  const i = Math.min(units.length - 1, Math.floor(Math.log(bytes) / Math.log(1024)));
  const value = bytes / 1024 ** i;
  return `${i === 0 ? value : value.toFixed(1)} ${units[i]}`;
}

function AttachmentRow({
  attachment,
  onDownload,
  onDelete,
}: {
  attachment: IssueAttachment;
  onDownload: (a: IssueAttachment) => Promise<void>;
  onDelete: (id: string) => Promise<void>;
}) {
  const [busy, setBusy] = useState<"download" | "delete" | null>(null);
  const name = attachment.originalFilename?.trim() || "attachment";

  const download = async () => {
    setBusy("download");
    try {
      await onDownload(attachment);
    } finally {
      setBusy(null);
    }
  };

  const remove = async () => {
    setBusy("delete");
    try {
      await onDelete(attachment.id);
      // No setBusy(null) on success — the row unmounts when the list revalidates.
    } catch {
      setBusy(null);
    }
  };

  return (
    <li
      className="flex items-center gap-2 px-3 py-2"
      style={{ border: "1px solid var(--edge)", borderRadius: 6 }}
    >
      <Paperclip size={14} className="flex-shrink-0" style={{ color: "var(--mute)" }} />
      <span className="flex-1 truncate text-sm text-[var(--ink)]">{name}</span>
      <span className="text-xs flex-shrink-0" style={{ color: "var(--mute)" }}>
        {formatBytes(attachment.byteSize)}
        {attachment.createdAt ? ` · ${timeAgo(attachment.createdAt)}` : ""}
      </span>
      <button
        type="button"
        onClick={() => void download()}
        disabled={busy !== null}
        aria-label={`Download ${name}`}
        className="inline-flex h-7 w-7 flex-shrink-0 items-center justify-center rounded transition-colors hover:bg-[var(--cream-deeper)] disabled:opacity-50"
        style={{ color: "var(--mute)" }}
      >
        <Download size={14} />
      </button>
      <button
        type="button"
        onClick={() => void remove()}
        disabled={busy !== null}
        aria-label={`Delete ${name}`}
        className="inline-flex h-7 w-7 flex-shrink-0 items-center justify-center rounded transition-colors hover:bg-[var(--cream-deeper)] disabled:opacity-50"
        style={{ color: "var(--danger)" }}
      >
        <Trash2 size={14} />
      </button>
    </li>
  );
}

export function IssueAttachmentsSection({ issueId }: { issueId: string }) {
  const { attachments, isLoading, isError, upload, remove, download } =
    useIssueAttachments(issueId);
  const inputRef = useRef<HTMLInputElement>(null);
  const [uploading, setUploading] = useState(false);
  const [dragOver, setDragOver] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);

  const handleFiles = async (files: FileList | null) => {
    if (!files || files.length === 0) return;
    setUploadError(null);
    setUploading(true);
    try {
      // Upload sequentially so each failure is attributable and the list
      // revalidates once per file.
      for (const file of Array.from(files)) {
        await upload(file);
      }
    } catch (e) {
      setUploadError(e instanceof Error ? e.message : "Upload failed.");
    } finally {
      setUploading(false);
      if (inputRef.current) inputRef.current.value = "";
    }
  };

  return (
    <section className="flex flex-col gap-3 pt-4" style={{ borderTop: "1px solid var(--edge)" }}>
      <h2 className="section-label">
        <b>Attachments</b>
      </h2>

      <input
        ref={inputRef}
        type="file"
        multiple
        className="sr-only"
        aria-label="Upload attachment"
        onChange={(e) => void handleFiles(e.target.files)}
      />

      <button
        type="button"
        onClick={() => inputRef.current?.click()}
        onDragOver={(e) => {
          e.preventDefault();
          setDragOver(true);
        }}
        onDragLeave={() => setDragOver(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDragOver(false);
          void handleFiles(e.dataTransfer.files);
        }}
        disabled={uploading}
        className="flex flex-col items-center justify-center gap-1 rounded px-3 py-5 text-sm transition-colors disabled:opacity-60"
        style={{
          border: "1px dashed var(--edge)",
          background: dragOver ? "var(--cream-deeper)" : "transparent",
          color: "var(--mute)",
        }}
      >
        <Upload size={16} />
        <span>{uploading ? "Uploading…" : "Drop a file here or click to upload"}</span>
      </button>

      {uploadError && (
        <p className="text-xs" style={{ color: "var(--danger)" }} role="alert">
          {uploadError}
        </p>
      )}
      {isError && (
        <p className="text-xs" style={{ color: "var(--danger)" }}>
          Couldn&apos;t load attachments.
        </p>
      )}
      {isLoading && attachments.length === 0 && !isError && (
        <p className="text-xs italic" style={{ color: "var(--mute)" }}>
          Loading attachments…
        </p>
      )}
      {!isLoading && attachments.length === 0 && !isError && (
        <p className="text-xs italic" style={{ color: "var(--mute)" }}>
          No attachments yet.
        </p>
      )}

      {attachments.length > 0 && (
        <ul className="flex flex-col gap-2">
          {attachments.map((a) => (
            <AttachmentRow
              key={a.id}
              attachment={a}
              onDownload={download}
              onDelete={remove}
            />
          ))}
        </ul>
      )}
    </section>
  );
}
