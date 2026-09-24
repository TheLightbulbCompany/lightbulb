"use client";

import { useCallback, useMemo } from "react";
import { useAuth } from "@clerk/nextjs";
import { useActivity as useSharedActivity, type ActivityPlatform } from "@lightbulb/shared";
import { useLiveEvents } from "@/hooks/useLiveEvents";
import { pageVisible } from "@/hooks/useRooms";

/** The viewer's Activity feed: the shared hook over the browser's live events and visibility. */
export function useActivity() {
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
  const platform = useMemo<ActivityPlatform>(() => ({
    userId: isLoaded ? userId ?? null : null,
    orgId: orgId ?? null,
    live,
    isVisible: pageVisible,
    onVisibilityChange,
    uuid: () => crypto.randomUUID(),
  }), [isLoaded, userId, orgId, live, onVisibilityChange]);
  return useSharedActivity(live.messaging, platform);
}
