"use client";

import { useEffect, useRef, useState } from "react";
import type { MessageAttachment } from "@lightbulb/shared";
import { FileViewerDialog } from "@/components/media/FileViewerDialog";

export function RoomImageAttachments({ roomId, attachments, download }: {
  roomId: string;
  attachments: readonly MessageAttachment[];
  download: (roomId: string, attachmentId: string) => Promise<Blob>;
}) {
  const [urls, setUrls] = useState<Record<string, string>>({});
  const [failed, setFailed] = useState(false);
  // -1 keeps the viewer closed; otherwise an index into `attachments`.
  const [openIndex, setOpenIndex] = useState(-1);
  // The thumbnail button that opened the viewer, so it can get focus back on close.
  const lastTriggerRef = useRef<HTMLButtonElement | null>(null);
  useEffect(() => {
    let active = true;
    const created: string[] = [];
    void Promise.all(attachments.map(async (attachment) => {
      const url = URL.createObjectURL(await download(roomId, attachment.id));
      if (!active) return URL.revokeObjectURL(url);
      created.push(url);
      setUrls((current) => ({ ...current, [attachment.id]: url }));
    })).catch(() => { if (active) setFailed(true); });
    return () => { active = false; created.forEach(URL.revokeObjectURL); };
  }, [attachments, download, roomId]);
  if (failed) return <p className="text-sm text-err">An image could not be loaded.</p>;
  return <>
    <div className="room-message-images">{attachments.map((attachment, i) => urls[attachment.id]
      ? <button key={attachment.id} type="button" onClick={(e) => { lastTriggerRef.current = e.currentTarget; setOpenIndex(i); }} aria-label={`View ${attachment.fileName}`} className="block cursor-pointer border-0 bg-transparent p-0">
        {/* eslint-disable-next-line @next/next/no-img-element */}<img src={urls[attachment.id]} alt={attachment.fileName} />
      </button>
      : <span key={attachment.id} role="status">Loading image…</span>)}</div>
    <FileViewerDialog
      attachments={attachments.map((attachment) => ({
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
