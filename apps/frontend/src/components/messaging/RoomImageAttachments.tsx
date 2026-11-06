"use client";

import { useCallback, useContext, useEffect, useRef, useState } from "react";
import { Download, FileArchive, FileSpreadsheet, FileText, Presentation, RotateCcw } from "lucide-react";
import { isVideoAttachmentType, type MessageAttachment, type MessageAttachmentContentType } from "@lightbulb/shared";
import { Button } from "@/components/ui/button";
import { FileViewerDialog } from "@/components/media/FileViewerDialog";
import { isPaneViewable } from "@/components/media/FileViewerContent";
import { openPanel } from "@/components/workspace/workspaceController";
import { OutputBackContext } from "@/components/creations/openOutput";
import { formatBytes } from "@/lib/utils";

const isImage = (attachment: MessageAttachment) => attachment.contentType.startsWith("image/");
const isVideo = (attachment: MessageAttachment) => isVideoAttachmentType(attachment.contentType);

// Everything here is downloaded on click and never fetched into the inline
// preview/viewer pipeline -- unlike images (lightbox) and the pane kinds
// below, which open the artifact pane. Label + icon per kind so a spreadsheet doesn't read as a
// document; the download behavior itself is identical for all of them.
const DOWNLOAD_ONLY: Partial<Record<MessageAttachmentContentType, { label: string; Icon: typeof FileArchive }>> = {
  "application/zip": { label: "ZIP", Icon: FileArchive },
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document": { label: "DOCX", Icon: FileText },
  "application/vnd.openxmlformats-officedocument.presentationml.presentation": { label: "PPTX", Icon: Presentation },
};
// Documents open in the artifact pane, never prefetched here (the pane fetches).
const PANE_LABEL: Partial<Record<MessageAttachmentContentType, string>> = {
  "application/pdf": "PDF",
  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "XLSX",
  "text/csv": "CSV", "text/html": "HTML", "text/markdown": "MD", "text/plain": "TXT",
};
const SPREADSHEET = new Set<string>(["text/csv", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"]);
const opensInPane = (attachment: MessageAttachment) => isPaneViewable(attachment.contentType);
const isDownloadOnly = (attachment: MessageAttachment) => attachment.contentType in DOWNLOAD_ONLY;

/**
 * Authenticated room attachments. The API bytes are converted to same-page blob
 * URLs so neither images nor documents leak bearer credentials into a new URL.
 * A video is the exception: up to 100 MB, past what the byte route can return,
 * so it plays from a short-lived presigned URL (`attachmentUrl`) instead.
 * The historical name is retained to avoid a broad call-site rename.
 */
export function RoomImageAttachments({ roomId, attachments, download, attachmentUrl }: {
  roomId: string;
  attachments: readonly MessageAttachment[];
  download: (roomId: string, attachmentId: string) => Promise<Blob>;
  attachmentUrl?: (roomId: string, attachmentId: string) => Promise<{ url: string }>;
}) {
  // Inside an open task (TaskPane), a doc opens with its way back to that task.
  const back = useContext(OutputBackContext);
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
    // Download-only kinds are intentionally not fetched into the preview
    // pipeline. They are downloaded only after an explicit click and never
    // passed to a viewer.
    const missing = attachments.filter((attachment) => !isDownloadOnly(attachment) && !opensInPane(attachment) && !urlsRef.current[attachment.id]);
    void Promise.all(missing.map(async (attachment) => {
      try {
        if (isVideo(attachment)) {
          if (!attachmentUrl) throw new Error("no attachment URL source");
          const { url } = await attachmentUrl(roomId, attachment.id);
          if (active) rememberUrl(attachment.id, url);
          return;
        }
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
  }, [attachments, attempt, attachmentUrl, download, rememberUrl, roomId]);

  // Only blob: URLs are ours to revoke; a video's is a presigned https URL.
  useEffect(() => () => Object.values(urlsRef.current).filter((url) => url.startsWith("blob:")).forEach(URL.revokeObjectURL), []);

  // A video's poster is the first image sent beside it (an agent attaches one with its render).
  const poster = attachments.find(isImage);
  const posterUrl = poster ? urls[poster.id] : undefined;

  function retry(attachmentId: string) {
    // A presigned URL expires (15 min): forget it so the retry mints a fresh one.
    delete urlsRef.current[attachmentId];
    setUrls((current) => { const next = { ...current }; delete next[attachmentId]; return next; });
    setFailed((current) => { const next = new Set(current); next.delete(attachmentId); return next; });
    setAttempt((value) => value + 1);
  }

  const viewableAttachments = attachments.filter((attachment) => !isDownloadOnly(attachment) && !opensInPane(attachment));

  function open(attachmentId: string, trigger: HTMLButtonElement) {
    lastTriggerRef.current = trigger;
    setOpenIndex(viewableAttachments.findIndex((attachment) => attachment.id === attachmentId));
  }

  async function downloadDirect(attachment: MessageAttachment) {
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
      if (isVideo(attachment)) return <figure key={attachment.id} className="room-video-attachment">
        {url && !loadFailed
          ? <video src={url} poster={posterUrl} controls playsInline preload="metadata" aria-label={attachment.fileName}
            onError={() => setFailed((current) => new Set(current).add(attachment.id))} />
          : loadFailed
            ? <Button type="button" variant="outline" onClick={() => retry(attachment.id)}><RotateCcw aria-hidden="true" /> Retry {attachment.fileName}</Button>
            : <span role="status">Loading video…</span>}
        <figcaption className="flex items-center gap-2 text-ui text-muted-foreground">
          <span className="min-w-0 truncate" title={attachment.fileName}>{attachment.fileName} · {formatBytes(attachment.size)}</span>
          {url && !loadFailed && <a href={url} target="_blank" rel="noopener noreferrer" aria-label={`Download ${attachment.fileName}`} className="inline-flex items-center gap-1"><Download aria-hidden="true" className="size-4" /> Download</a>}
        </figcaption>
      </figure>;
      const downloadOnly = DOWNLOAD_ONLY[attachment.contentType];
      if (downloadOnly) {
        const isDownloading = downloading.has(attachment.id);
        const { label, Icon } = downloadOnly;
        return <button key={attachment.id} type="button" className="room-file-card" disabled={isDownloading}
          aria-label={`${loadFailed ? "Retry download" : "Download"} ${attachment.fileName}`}
          onClick={() => void downloadDirect(attachment)}>
          <Icon aria-hidden="true" />
          <span className="min-w-0 flex-1"><strong className="block truncate" title={attachment.fileName}>{attachment.fileName}</strong>
            <span className="block text-ui text-muted-foreground">{label} · {formatBytes(attachment.size)}</span>
            <span className={loadFailed ? "block text-ui text-err" : "block text-ui text-muted-foreground"}>{loadFailed ? "Download failed — try again" : isDownloading ? "Downloading…" : "Download file"}</span></span>
          {loadFailed ? <RotateCcw aria-hidden="true" /> : <Download aria-hidden="true" />}
        </button>;
      }
      return <button key={attachment.id} type="button" className="room-link-card room-doc-card" aria-label={`Open ${attachment.fileName}`}
        onClick={() => openPanel("artifact", { src: "attachment", room: roomId, id: attachment.id, name: attachment.fileName, type: attachment.contentType, ...(back ? { back } : {}) }, { pushUrl: true })}>
        <span className="room-link-card-body">
          <span className="room-doc-card-head">{SPREADSHEET.has(attachment.contentType) ? <FileSpreadsheet size={16} aria-hidden="true" /> : <FileText size={16} aria-hidden="true" />}<span className="room-link-card-title">{attachment.fileName}</span></span>
          <span className="room-link-card-meta">{PANE_LABEL[attachment.contentType]} · {formatBytes(attachment.size)}</span>
          <span className="room-doc-card-foot"><span className="room-doc-card-open" aria-hidden="true">Open</span></span>
        </span>
      </button>;
    })}</div>
    <FileViewerDialog
      attachments={viewableAttachments.map((attachment) => ({
        id: attachment.id,
        fileName: attachment.fileName,
        contentType: attachment.contentType,
        size: attachment.size,
        url: urls[attachment.id],
        poster: isVideo(attachment) ? posterUrl : undefined,
      }))}
      index={openIndex}
      onIndexChange={setOpenIndex}
      onOpenChange={(next) => { if (!next) setOpenIndex(-1); }}
      restoreFocusRef={lastTriggerRef}
    />
  </>;
}
