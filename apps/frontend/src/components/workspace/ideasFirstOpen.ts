"use client";

// Ideas opens by itself the first time a person opens Chat in a ready workspace on a wide screen
// (owner, 2026-10-15): once per person per workspace. If browser storage refuses the flag, it
// never opens by itself (it could not remember that it already did).
import { useEffect } from "react";
import { getWorkspaceSnapshot, openPanel, useWorkspaceState } from "./workspaceController";

export const ideasFirstOpenKey = (userId: string, scope: string) => `lightbulb.ideas.first-open.${userId}:${scope}`;

/** Opens the Ideas pane if this is the first time; true when it opened. */
export function openIdeasOnFirstUse(key: string): boolean {
  const snapshot = getWorkspaceSnapshot();
  if (!snapshot.ready || snapshot.place !== "chat" || snapshot.pane) return false;
  if (!window.matchMedia?.("(min-width: 600px)").matches) return false;
  try {
    if (window.localStorage.getItem(key)) return false;
    window.localStorage.setItem(key, "1");
  } catch {
    return false;
  }
  openPanel("ideas");
  return true;
}

export function useIdeasFirstOpen(chatReady: boolean, userId: string | null | undefined, scope: string | null | undefined): void {
  const { ready, place } = useWorkspaceState();
  useEffect(() => {
    if (chatReady && ready && place === "chat" && userId && scope) openIdeasOnFirstUse(ideasFirstOpenKey(userId, scope));
  }, [chatReady, ready, place, userId, scope]);
}
