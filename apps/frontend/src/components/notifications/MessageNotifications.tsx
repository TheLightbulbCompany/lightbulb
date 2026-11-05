"use client";

import { openTaskPane } from "@/components/workspace/workspaceController";
import { useEffect, useRef } from "react";
import { useAuth } from "@clerk/nextjs";
import { useLiveEvents } from "@/hooks/useLiveEvents";
import { capture } from "@/lib/analytics";
import { desktopDeviceId, isDesktopApp, nativeNotify, notificationPermission, onDesktopToast, onSessionNoticeClick, onTaskNoticeClick } from "@/lib/desktopNotify";
import { notifySuccess } from "@/lib/notify";
import type { RoomSummary } from "@lightbulb/shared";
import { claimRunNotice, setRoomView } from "./roomView";

export function MessageNotifications(props: {
  activeRoomId: string | null;
  threadId: string | null;
  roomVisible: boolean;
  /** Rooms on screen in a side column (a session's chat, an app's conversation in Canvas), each beside
   * whatever the main view shows. They show thread replies inline: a reply there is on screen too. */
  sideRoomIds?: readonly string[];
  onOpen: (roomId: string, threadId: string | null, messageId: string) => void;
  /** The shared session on screen, if any: its own notifications are not shown while you watch it. */
  activeSessionId?: string | null;
  onOpenSession?: (sessionId: string) => void;
  /** The room list, so run notifications can name a run's room. */
  rooms?: RoomSummary[];
}): null {
  const { userId } = useAuth();
  const { onEvent } = useLiveEvents();
  const current = useRef(props);
  useEffect(() => { current.current = props; });

  const { activeRoomId, roomVisible, rooms, sideRoomIds } = props;
  useEffect(() => {
    setRoomView({
      visibleRoomId: roomVisible ? activeRoomId : null,
      sideRoomIds,
      rooms: new Map((rooms ?? []).map((room) => [room.roomId, { kind: room.kind, name: room.name }])),
    });
  }, [activeRoomId, roomVisible, rooms, sideRoomIds]);
  useEffect(() => () => setRoomView({ visibleRoomId: null, rooms: new Map() }), []);

  // The desktop app's own notices, when macOS notifications are off.
  useEffect(() => onDesktopToast((title, body, open) => {
    notifySuccess(title, {
      dedupeKey: `desktop:${body}`,
      description: body,
      ...(open ? { action: { label: "Open", onClick: open } } : {}),
    });
  }), []);

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

  // A Mac request notification (held message, agent ask) opens the session it is about.
  useEffect(() => onSessionNoticeClick((sessionId) => current.current.onOpenSession?.(sessionId)), []);
  // A task-question notification (tag `task:<id>`) opens its thread pane.
  useEffect(() => onTaskNoticeClick(openTaskPane), []);

  useEffect(() => {
    const seen = new Set<string>();
    let stopped = false;
    const unlisten = onEvent((event) => {
      if (event.type === "workspace.session.attention") {
        // One of YOUR shared sessions is waiting for you or finished: the phone's banner, verbatim.
        const { sessionId, state, push, level, sound, onlyUnfocused } = event.payload;
        if (seen.has(event.eventId)) return;
        seen.add(event.eventId);
        // Off and badge-only never banner (the Dock and tab count carry badge-only).
        if (level !== undefined && level !== "banner") return;
        const focused = document.visibilityState === "visible" && document.hasFocus();
        const watching = current.current.activeSessionId === sessionId && focused;
        if (watching) return;
        // In a browser the page's focus is the app's; the desktop app asks its own windows (native_notify).
        if (onlyUnfocused && focused && !isDesktopApp()) return;
        const show = () => {
          capture("notification_shown", { channel: "web", kind: `session_${state}` });
          const onClick = () => {
            capture("notification_clicked", { channel: "web", kind: `session_${state}` });
            current.current.onOpenSession?.(sessionId);
          };
          void nativeNotify(push.title, push.body, { onClick, tag: event.eventId, sound, onlyUnfocused }).then((shown) => {
            if (shown || stopped) return;
            notifySuccess(push.title, {
              dedupeKey: `session:${event.eventId}`,
              description: push.body || undefined,
              action: { label: "Open", onClick },
            });
          });
        };
        const { held, hostDeviceId } = event.payload;
        if (!held || !hostDeviceId || !isDesktopApp()) { show(); return; }
        // The Mac that hosts a held send posts its own notification, with Allow and Decline,
        // when macOS lets it; with notifications off there, the window's toast still shows.
        void Promise.all([desktopDeviceId(), notificationPermission()]).then(([here, allowed]) => {
          if (!stopped && (here !== hostDeviceId || allowed !== "granted")) show();
        });
        return;
      }
      // `push` is the projector's banner for this reader — the exact text the
      // phone shows — present only when it notifies an unmuted reader.
      // The event validator tolerates extra fields, so the banner's shape is checked here.
      if (event.type !== "room.message.available") return;
      const push = event.payload.push;
      if (!push || typeof push.title !== "string" || typeof push.body !== "string") return;
      const { roomId, messageId, threadRootMessageId } = event.payload;
      const { title, subtitle, body, threadId, data } = push;
      if (seen.has(messageId)) return;
      seen.add(messageId);
      if (seen.size > 1000) seen.delete(seen.values().next().value!);
      const now = current.current;
      const inMain = now.roomVisible && now.activeRoomId === roomId && (!threadRootMessageId || now.threadId === threadRootMessageId);
      const watched = (inMain || !!now.sideRoomIds?.includes(roomId)) && document.visibilityState === "visible" && document.hasFocus();
      if (watched) return;
      if (data.runId && !claimRunNotice(data.runId)) return;
      capture("notification_shown", { channel: "web", room_kind: data.roomKind });
      const onClick = () => {
        capture("notification_clicked", { channel: "web", room_kind: data.roomKind });
        current.current.onOpen(roomId, threadRootMessageId, messageId);
      };
      // Native (desktop app, or web with permission) replaces the toast.
      void nativeNotify(title, body, { onClick, tag: messageId, subtitle, threadId }).then((shown) => {
        if (shown || stopped) return;
        // Line 1 who · where, line 2 the message. (An older projector still
        // sends the where as a separate subtitle.)
        notifySuccess(subtitle ? `${title} · ${subtitle}` : title, {
          dedupeKey: `message:${messageId}`,
          description: body,
          action: { label: "View", onClick },
        });
      });
    });
    return () => { stopped = true; unlisten(); };
  }, [onEvent]);
  return null;
}
