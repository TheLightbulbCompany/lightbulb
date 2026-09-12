"use client";

import { useEffect, useState } from "react";
import type { MessageAttachment } from "@lightbulb/shared";

export function RoomImageAttachments({ roomId, attachments, download }: {
  roomId: string;
  attachments: readonly MessageAttachment[];
  download: (roomId: string, attachmentId: string) => Promise<Blob>;
}) {
  const [urls, setUrls] = useState<Record<string, string>>({});
  const [failed, setFailed] = useState(false);
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
  return <div className="room-message-images">{attachments.map((attachment) => urls[attachment.id]
    ? <a key={attachment.id} href={urls[attachment.id]} target="_blank" rel="noopener noreferrer" aria-label={`Open ${attachment.fileName}`}>
      {/* eslint-disable-next-line @next/next/no-img-element */}<img src={urls[attachment.id]} alt={attachment.fileName} />
    </a>
    : <span key={attachment.id} role="status">Loading image…</span>)}</div>;
}
