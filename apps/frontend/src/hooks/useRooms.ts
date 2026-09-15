"use client";

import { useMemo } from "react";
import { useAuth } from "@clerk/nextjs";
import { useRooms as useSharedRooms, type RoomsPlatform } from "@lightbulb/shared";
import { useLiveEvents } from "@/hooks/useLiveEvents";

// The browser's half of RoomsPlatform.isVisible: document visible, window focused.
const pageVisible = () => document.visibilityState === "visible" && document.hasFocus();
const onPageVisibilityChange = (listener: () => void) => {
  window.addEventListener("focus", listener);
  document.addEventListener("visibilitychange", listener);
  return () => {
    window.removeEventListener("focus", listener);
    document.removeEventListener("visibilitychange", listener);
  };
};

/** `roomVisible` false: something covers the room (a session in focus), so it must not auto-read. */
export function useRooms(initialAgentId?: string | null, subscribeToRoom = true, roomVisible = true) {
  const { isLoaded, userId, orgId } = useAuth();
  const live = useLiveEvents();
  const platform = useMemo<RoomsPlatform>(() => ({
    userId: isLoaded ? userId ?? null : null,
    orgId: orgId ?? null,
    live,
    isVisible: pageVisible,
    onVisibilityChange: onPageVisibilityChange,
    uuid: () => crypto.randomUUID(),
  }), [isLoaded, userId, orgId, live]);
  return useSharedRooms(platform, { initialAgentId, subscribeToRoom, visible: roomVisible });
}
