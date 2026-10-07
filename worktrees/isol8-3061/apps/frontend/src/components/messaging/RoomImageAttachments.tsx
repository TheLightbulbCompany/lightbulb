"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Download, FileArchive, FileText, RotateCcw } from "lucide-react";
import type { MessageAttachment } from "@lightbulb/shared";
import { Button } from "@/components/ui/button";
import { FileViewerDialog } from "@/components/media/FileViewerDialog";
import { formatBytes } from "@/lib/utils";

const isImage = (attachment: MessageAttachment) => attachment.contentType.startsWith("image/");
const isZip = (attachment: MessageAttachment) => attachment.contentType === "application/zip";

/**
 * Authenticated room attachments. The API bytes are converted to same-page blob
 * URLs so neither images nor documents leak bearer credentials into a new URL.
 * The historical name is retained to avoid a broad call-site rename.
 */
export function RoomImageAttachments({ roomId, attachments, download }: {
  roomId: string;
  attachments: readonly MessageAttachment[];
  download: (roomId: string, attachmentId: string) => Promise<Blob>;
}) {
  const [urls, setUrls] = useState<Record<string, string>>({});
  const [failed, setFailed] = useState<Set<string>>(new Set());
  const [downloading, setDownloading] = useState<Set<string>>(new Set());
  const [attempt, setAttempt] = useState(0);
  // -1 keeps the viewer closed; otherwise an index into previewable attachments.
  const [openIndex, setOpenIndex] = useState(-1);
  // The attachment control that opened the viewer, so it can get focus back on close.
  const lastTriggerRef = useRef<HTMLButtonElement | null>(null);
  const urlsRef = useRef<Record<string, string>>({});

  const rememberUrl = useCallback((id: string, url: string) => {
    urlsRef.current[id] = url;
    setUrls((current) => ({ ...current, [id]: url }));
  }, []);

  useEffect(() => {
    let active = true;
    // ZIPs are intentionally not fetched into the preview pipeline. They are
    // downloaded only after an explicit click and never passed to a viewer.
    const missing = attachments.filter((attachment) => !isZip(attachment) && !urlsRef.current[attachment.id]);
    void Promise.all(missing.map(async (attachment) => {
      try {
        const url = URL.createObjectURL(await download(roomId, attachment.id));
        if (!active) return URL.revokeObjectURL(url);
        rememberUrl(attachment.id, url);
        setFailed((current) => {
          const next = new Set(current);
          next.delete(attachment.id);
          return next;
        });
      } catch {
        if (active) setFailed((current) => new Set(current).add(attachment.id));
      }
    }));
    return () => { active = false; };
  }, [attachments, attempt, download, rememberUrl, roomId]);

  useEffect(() => () => Object.values(urlsRef.current).forEach(URL.revokeObjectURL), []);

  const viewableAttachments = attachments.filter((attachment) => !isZip(attachment));

  function open(attachmentId: string, trigger: HTMLButtonElement) {
    lastTriggerRef.current = trigger;
    setOpenIndex(viewableAttachments.findIndex((attachment) => attachment.id === attachmentId));
  }

  async function downloadZip(attachment: MessageAttachment) {
    setDownloading((current) => new Set(current).add(attachment.id));
    setFailed((current) => { const next = new Set(current); next.delete(attachment.id); return next; });
    try {
      const url = URL.createObjectURL(await download(roomId, attachment.id));
      const link = document.createElement("a");
      link.href = url;
      link.download = attachment.fileName;
      document.body.append(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
    } catch {
      setFailed((current) => new Set(current).add(attachment.id));
    } finally {
      setDownloading((current) => { const next = new Set(current); next.delete(attachment.id); return next; });
    }
  }

  return <>
    <div className="room-message-attachments">{attachments.map((attachment) => {
      const url = urls[attachment.id];
      const loadFailed = failed.has(attachment.id);
      if (isImage(attachment)) return url
        ? <button key={attachment.id} type="button" onClick={(event) => open(attachment.id, event.currentTarget)} aria-label={`View ${attachment.fileName}`} className="block cursor-pointer border-0 bg-transparent p-0">
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img src={url} alt={attachment.fileName} />
        </button>
        : <span key={attachment.id}>{loadFailed
          ? <Button type="button" variant="outline" onClick={() => { setFailed((current) => { const next = new Set(current); next.delete(attachment.id); return next; }); setAttempt((value) => value + 1); }}><RotateCcw aria-hidden="true" /> Retry {attachment.fileName}</Button>
          : <span role="status">Loading image…</span>}</span>;
      if (isZip(attachment)) {
        const isDownloading = downloading.has(attachment.id);
        return <button key={attachment.id} type="button" className="room-file-card" disabled={isDownloading}
          aria-label={`${loadFailed ? "Retry download" : "Download"} ${attachment.fileName}`}
          onClick={() => void downloadZip(attachment)}>
          <FileArchive aria-hidden="true" />
          <span className="min-w-0 flex-1"><strong className="block truncate" title={attachment.fileName}>{attachment.fileName}</strong>
            <span className="block text-sm text-muted-foreground">ZIP · {formatBytes(attachment.size)}</span>
            <span className={loadFailed ? "block text-sm text-err" : "block text-sm text-muted-foreground"}>{loadFailed ? "Download failed — try again" : isDownloading ? "Downloading…" : "Download file"}</span></span>
          {loadFailed ? <RotateCcw aria-hidden="true" /> : <Download aria-hidden="true" />}
        </button>;
      }
      return <button key={attachment.id} type="button" className="room-file-card" disabled={!url && !loadFailed}
        aria-label={loadFailed ? `Retry ${attachment.fileName}` : url ? `Open ${attachment.fileName}` : undefined}
        onClick={(event) => {
          if (loadFailed) {
            setFailed((current) => { const next = new Set(current); next.delete(attachment.id); return next; });
            setAttempt((value) => value + 1);
          } else if (url) open(attachment.id, event.currentTarget);
        }}>
        <FileText aria-hidden="true" />
        <span className="min-w-0 flex-1"><strong className="block truncate" title={attachment.fileName}>{attachment.fileName}</strong>
          <span className="block text-sm text-muted-foreground">PDF · {formatBytes(attachment.size)}</span>
          <span className={loadFailed ? "block text-sm text-err" : "block text-sm text-muted-foreground"}>{loadFailed ? "Could not load file" : url ? "Open file" : "Loading…"}</span></span>
        {loadFailed && <RotateCcw aria-hidden="true" />}
      </button>;
    })}</div>
    <FileViewerDialog
      attachments={viewableAttachments.map((attachment) => ({
        id: attachment.id,
        fileName: attachment.fileName,
        contentType: attachment.contentType,
        size: attachment.size,
        url: urls[attachment.id],
      }))}
      index={openIndex}
      onIndexChange={setOpenIndex}
      onOpenChange={(next) => { if (!next) setOpenIndex(-1); }}
      restoreFocusRef={lastTriggerRef}
    />
  </>;
}
