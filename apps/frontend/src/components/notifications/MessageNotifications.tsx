"use client";

import { useEffect, useRef } from "react";
import { useAuth } from "@clerk/nextjs";
import { useLiveEvents } from "@/hooks/useLiveEvents";
import { capture } from "@/lib/analytics";
import { isDesktopApp, nativeNotify, notificationPermission } from "@/lib/desktopNotify";
import { notifySuccess } from "@/lib/notify";

export function MessageNotifications(props: {
  activeRoomId: string | null;
  threadId: string | null;
  roomVisible: boolean;
  onOpen: (roomId: string, threadId: string | null) => void;
}): null {
  const { userId } = useAuth();
  const { onEvent } = useLiveEvents();
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
      // `push` is the projector's banner for this reader — the exact text the
      // phone shows — present only when it notifies an unmuted reader.
      // The event validator tolerates extra fields, so the banner's shape is checked here.
      if (event.type !== "room.message.available") return;
      const push = event.payload.push;
      if (!push || typeof push.title !== "string" || typeof push.body !== "string") return;
      const { roomId, messageId, threadRootMessageId } = event.payload;
      const { title, subtitle, body, data } = push;
      if (seen.has(messageId)) return;
      seen.add(messageId);
      if (seen.size > 1000) seen.delete(seen.values().next().value!);
      const watched = current.current.roomVisible && current.current.activeRoomId === roomId &&
        (!threadRootMessageId || current.current.threadId === threadRootMessageId) &&
        document.visibilityState === "visible" && document.hasFocus();
      if (watched) return;
      capture("notification_shown", { channel: "web", room_kind: data.roomKind });
      const onClick = () => {
        capture("notification_clicked", { channel: "web", room_kind: data.roomKind });
        current.current.onOpen(roomId, threadRootMessageId);
      };
      // Native (desktop app, or web with permission) replaces the toast.
      void nativeNotify(title, body, { onClick, tag: messageId, subtitle }).then((shown) => {
        if (shown || stopped) return;
        notifySuccess(`${title} · ${subtitle}: ${body}`, {
          dedupeKey: `message:${messageId}`,
          action: { label: "View", onClick },
        });
      });
    });
    return () => { stopped = true; unlisten(); };
  }, [onEvent]);
  return null;
}
