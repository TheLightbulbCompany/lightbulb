"use client";

import { useCallback } from "react";
import { useAuth } from "@clerk/nextjs";
import { useIdeas as useSharedIdeas } from "@lightbulb/shared";
import { useLiveEvents } from "@/hooks/useLiveEvents";
import { notifyApiError } from "@/lib/notify";
import { openMessagePanel, openTaskPane } from "@/components/workspace/workspaceController";

/** The workspace's ideas (shared hook), plus Start that opens what the idea started. */
export function useIdeas() {
  const { isLoaded, userId, orgId } = useAuth();
  const live = useLiveEvents();
  const ideas = useSharedIdeas(live.messaging, isLoaded && userId ? `${userId}:${orgId ?? userId}` : null);
  const { start, dismiss } = ideas;
  /** Starts the idea and opens it: the request it started (super-agent), else its DM. Null once open; otherwise
   *  what went wrong, for the caller to show beside a button that can be pressed again. */
  const startAndOpen = useCallback(async (ideaId: string): Promise<string | null> => {
    try {
      const { roomId, taskId } = await start(ideaId);
      if (taskId) openTaskPane(taskId);
      else openMessagePanel(roomId);
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
