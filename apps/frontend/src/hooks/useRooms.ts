"use client";

import { useCallback, useMemo } from "react";
import { useAuth } from "@clerk/nextjs";
import { useRooms as useSharedRooms, type RoomsPlatform } from "@lightbulb/shared";
import { useLiveEvents } from "@/hooks/useLiveEvents";

// The browser's half of RoomsPlatform. Both callbacks are []-stable so the
// shared hook's visibility effect subscribes once, not once per render.
const isVisible = () => document.visibilityState === "visible" && document.hasFocus();

export function useRooms(initialAgentId?: string | null, subscribeToRoom = true) {
  const { isLoaded, userId, orgId } = useAuth();
  const live = useLiveEvents();
  const onVisibilityChange = useCallback((listener: () => void) => {
    window.addEventListener("focus", listener);
    document.addEventListener("visibilitychange", listener);
    return () => {
      window.removeEventListener("focus", listener);
      document.removeEventListener("visibilitychange", listener);
    };
  }, []);
  const platform = useMemo<RoomsPlatform>(() => ({
    userId: isLoaded ? userId ?? null : null,
    orgId: orgId ?? null,
    live,
    isVisible,
    onVisibilityChange,
    uuid: () => crypto.randomUUID(),
  }), [isLoaded, userId, orgId, live, onVisibilityChange]);
  return useSharedRooms(platform, { initialAgentId, subscribeToRoom });
}
