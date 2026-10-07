"use client";

import { useEffect, useRef } from "react";
import { useAuth } from "@clerk/nextjs";
import { useLiveEvents } from "@/hooks/useLiveEvents";
import { capture } from "@/lib/analytics";
import { isDesktopApp, nativeNotify, notificationPermission } from "@/lib/desktopNotify";
import { notifySuccess } from "@/lib/notify";
import { plainPreview } from "@/lib/plainPreview";

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

  // The OS prompt is the ask; nothing in the app asks in front of it. macOS
  // can raise it unprompted, so the desktop app does, once per person on this
  // device — and whatever they answer (or ignore), the provisional
  // registration at launch keeps Lightbulb listed under System Settings →
  // Notifications. A browser only prompts from a user gesture, so there it
  // waits for the first click or keypress.
  useEffect(() => {
    if (!userId) return;
    let cancelled = false;
    let stopWaiting = () => {};
    void notificationPermission().then((state) => {
      if (cancelled || state !== "default") return; // decided, or no native path yet
      if (isDesktopApp()) {
        const key = `lightbulb.notifications.asked.${userId}`;
        try {
          if (localStorage.getItem(key)) return;
          localStorage.setItem(key, "1");
        } catch {
          return;
        }
        void notificationPermission(true);
        return;
      }
      const requestOnce = () => {
        stopWaiting();
        void notificationPermission(true);
      };
      document.addEventListener("pointerdown", requestOnce);
      document.addEventListener("keydown", requestOnce);
      stopWaiting = () => {
        document.removeEventListener("pointerdown", requestOnce);
        document.removeEventListener("keydown", requestOnce);
      };
    });
    return () => { cancelled = true; stopWaiting(); };
  }, [userId]);

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
        .then(async ([room, page]) => {
          if (stopped || watched() || room.muted || messageSeq <= room.lastReadSeq) return;
          const message = page.messages.find((item) => item.messageId === messageId);
          if (!message || (message.author.kind === "human" && message.author.id === userId)) return;
          const title = room.kind === "channel"
            ? `${message.author.displayName} in #${room.name ?? "channel"}`
            : message.author.displayName;
          const body = plainPreview(message.text).slice(0, 180) || "Sent an image";
          capture("notification_shown", { channel: "web", room_kind: room.kind });
          const onClick = () => {
            capture("notification_clicked", { channel: "web", room_kind: room.kind });
            current.current.onOpen(roomId, threadRootMessageId);
          };
          // Native (desktop app, or web with permission) replaces the toast.
          if (await nativeNotify(title, body, { onClick, tag: messageId })) return;
          notifySuccess(`${title}: ${body}`, {
            dedupeKey: `message:${messageId}`,
            action: { label: "View", onClick },
          });
        }).catch(() => {});
    });
    return () => { stopped = true; unlisten(); };
  }, [onEvent, messaging, userId, orgId]);
  return null;
}
