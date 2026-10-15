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
  /** Starts the idea and opens its DM. Null once the DM is open; otherwise what went wrong, for
   *  the detail to show beside a Let's do it that can be pressed again. */
  const startAndOpen = useCallback(async (ideaId: string): Promise<string | null> => {
    try {
      const { roomId } = await start(ideaId);
      openMessagePanel(roomId);
      return null;
    } catch (err) {
      return err instanceof Error && err.message ? err.message : "The idea could not start. Try again.";
    }
  }, [start]);
  const dismissQuietly = useCallback((ideaId: string) => {
    dismiss(ideaId).catch((err) => notifyApiError(err, "The idea could not be hidden. Try again."));
  }, [dismiss]);
  return { ...ideas, startAndOpen, dismiss: dismissQuietly };
}
