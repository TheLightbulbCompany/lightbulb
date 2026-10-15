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
  const { start, dismiss, refresh } = ideas;
  const startAndOpen = useCallback(async (ideaId: string) => {
    try {
      const { roomId } = await start(ideaId);
      openMessagePanel(roomId);
    } catch (err) {
      // 409: this idea is already starting (another Start on screen). Its DM is the answer.
      if ((err as { status?: number }).status === 409) {
        const mine = (await refresh().catch(() => undefined))?.running
          .find((r) => r.ideaId === ideaId && r.starterId === userId && r.roomId);
        if (mine?.roomId) openMessagePanel(mine.roomId);
        return;
      }
      notifyApiError(err, "The idea could not start. Try again.");
    }
  }, [start, refresh, userId]);
  const dismissQuietly = useCallback((ideaId: string) => {
    dismiss(ideaId).catch((err) => notifyApiError(err, "The idea could not be hidden. Try again."));
  }, [dismiss]);
  return { ...ideas, userId: userId ?? null, startAndOpen, dismiss: dismissQuietly };
}
