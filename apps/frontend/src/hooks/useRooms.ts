"use client";

import { useCallback, useEffect, useMemo, useRef } from "react";
import { useAuth } from "@clerk/nextjs";
import { useRooms as useSharedRooms, type RoomsPlatform, type UseRoomsResult } from "@lightbulb/shared";
import { useLiveEvents } from "@/hooks/useLiveEvents";

// The browser's half of RoomsPlatform.isVisible: document visible, window focused.
export const pageVisible = () => document.visibilityState === "visible" && document.hasFocus();

/** `roomVisible` false: something covers the room (a session in focus), so it must not auto-read.
 *  `autoSelect` false: a second room slot (a session's chat) that never picks a listed room on its own.
 *  `needsFocus` false: that slot reads while the page is visible, focused or not -- a person watching a
 *  session beside another window is reading its chat (prod 2026-10-07: the count came back after a reload). */
export function useRooms(initialAgentId?: string | null, subscribeToRoom = true, roomVisible = true, autoSelect = true, needsFocus = true) {
  const { isLoaded, userId, orgId } = useAuth();
  const live = useLiveEvents();
  // Both callbacks are []-stable so the shared hook's visibility effect
  // subscribes once, not once per render; `roomVisible` is read through a ref.
  const roomVisibleRef = useRef(roomVisible);
  const listeners = useRef(new Set<() => void>());
  const isVisible = useCallback(() => roomVisibleRef.current
    && (needsFocus ? pageVisible() : document.visibilityState === "visible"), [needsFocus]);
  const onVisibilityChange = useCallback((listener: () => void) => {
    listeners.current.add(listener);
    window.addEventListener("focus", listener);
    document.addEventListener("visibilitychange", listener);
    return () => {
      listeners.current.delete(listener);
      window.removeEventListener("focus", listener);
      document.removeEventListener("visibilitychange", listener);
    };
  }, []);
  // Declared before the shared hook, so its read check in the same commit sees
  // the new value. Uncovering the room is a visibility change like a window
  // focus: it re-runs that check.
  useEffect(() => {
    roomVisibleRef.current = roomVisible;
    if (roomVisible) listeners.current.forEach((listener) => listener());
  }, [roomVisible]);
  const platform = useMemo<RoomsPlatform>(() => ({
    userId: isLoaded ? userId ?? null : null,
    orgId: orgId ?? null,
    live,
    isVisible,
    onVisibilityChange,
    uuid: () => crypto.randomUUID(),
  }), [isLoaded, userId, orgId, live, isVisible, onVisibilityChange]);
  // The package can be consumed from a previously built workspace declaration
  // during rolling CI installs. Keep the app boundary typed to the source
  // contract (bounded history + older-page loading) that the runtime implements.
  return useSharedRooms(
    platform,
    { initialAgentId, subscribeToRoom, autoSelect, boundedHistory: true } as Parameters<typeof useSharedRooms>[1],
  ) as UseRoomsResult & { loadOlder: () => Promise<void> };
}
