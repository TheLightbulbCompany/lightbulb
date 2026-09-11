"use client";

import { useEffect, useRef } from "react";
import { useAuth } from "@clerk/nextjs";
import { useLiveEvents } from "@/hooks/useLiveEvents";
import { nativeNotify } from "@/lib/desktopNotify";
import { notifySuccess } from "@/lib/notify";

export function MessageNotifications(props: {
  activeRoomId: string | null;
  threadId: string | null;
  roomVisible: boolean;
  onOpen: (roomId: string, threadId: string | null) => void;
}): null {
  const { userId, orgId } = useAuth();
  const { onEvent, messaging } = useLiveEvents();
  const current = useRef(props);
  useEffect(() => { current.current = props; });

  useEffect(() => {
    const seen = new Set<string>();
    let stopped = false;
    const unlisten = onEvent((event) => {
      if (event.type !== "room.message.available" || !event.payload.notify) return;
      const { roomId, messageId, messageSeq, threadRootMessageId } = event.payload;
      if (seen.has(messageId)) return;
      seen.add(messageId);
      if (seen.size > 1000) seen.delete(seen.values().next().value!);
      const watched = () => current.current.roomVisible && current.current.activeRoomId === roomId &&
        (!threadRootMessageId || current.current.threadId === threadRootMessageId) &&
        document.visibilityState === "visible" && document.hasFocus();
      if (watched()) return;
      // The private event contains no message content. HTTP checks current
      // membership again before we display anything from the conversation.
      void Promise.all([messaging.getRoom(roomId), messaging.listMessages(roomId, messageSeq - 1, 1)])
        .then(([room, page]) => {
          if (stopped || watched() || messageSeq <= room.lastReadSeq) return;
          const message = page.messages.find((item) => item.messageId === messageId);
          if (!message || (message.author.kind === "human" && message.author.id === userId)) return;
          const title = room.kind === "channel"
            ? `${message.author.displayName} in #${room.name ?? "channel"}`
            : message.author.displayName;
          const body = message.text.slice(0, 180) || "Sent an image";
          notifySuccess(`${title}: ${body}`, {
            dedupeKey: `message:${messageId}`,
            action: { label: "View", onClick: () => current.current.onOpen(roomId, threadRootMessageId) },
          });
          nativeNotify(title, body);
        }).catch(() => {});
    });
    return () => { stopped = true; unlisten(); };
  }, [onEvent, messaging, userId, orgId]);
  return null;
}
