"use client";

import { useCallback } from "react";
import { useAuth } from "@clerk/nextjs";
import { useIdeas as useSharedIdeas } from "@lightbulb/shared";
import { useLiveEvents } from "@/hooks/useLiveEvents";
import { notifyApiError } from "@/lib/notify";
import { openMessagePanel } from "@/components/workspace/workspaceController";

/** The workspace's ideas (shared hook), plus Start that opens the idea's DM in the main view. */
export function useIdeas() {
  const { isLoaded, userId, orgId } = useAuth();
  const live = useLiveEvents();
  const ideas = useSharedIdeas(live.messaging, isLoaded && userId ? `${userId}:${orgId ?? userId}` : null);
  const { start, dismiss } = ideas;
  const startAndOpen = useCallback(async (ideaId: string) => {
    try {
      const { roomId } = await start(ideaId);
      openMessagePanel(roomId);
    } catch (err) {
      notifyApiError(err, "The idea could not start. Try again.");
    }
  }, [start]);
  const dismissQuietly = useCallback((ideaId: string) => {
    dismiss(ideaId).catch((err) => notifyApiError(err, "The idea could not be hidden. Try again."));
  }, [dismiss]);
  return { ...ideas, startAndOpen, dismiss: dismissQuietly };
}
