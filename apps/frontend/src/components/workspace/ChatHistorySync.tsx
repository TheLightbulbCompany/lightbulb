"use client";

import { useEffect, useRef } from "react";
import { useMessagingWorkspace } from "@/components/messaging/MessagingWorkspaceContext";
import { recordChatView, resetChatView } from "./chatHistory";
import { parseCommand } from "./urlChannel";
import { activateMessagesPanel } from "./workspaceController";

/** Puts the person's room, DM and session picks into browser history (desktop shell only). */
export function ChatHistorySync(): null {
  const { activeRoomId, sessionFocus, explicitPicks } = useMessagingWorkspace();
  const seenPicks = useRef(explicitPicks.current);
  const sessionId = sessionFocus?.sessionId ?? null;
  // The new-session composer and the all-sessions list are not places to go back to.
  const composing = !!sessionFocus && !sessionFocus.sessionId;
  useEffect(() => {
    if (composing) return;
    const explicit = explicitPicks.current !== seenPicks.current || !!sessionId;
    seenPicks.current = explicitPicks.current;
    recordChatView({ roomId: activeRoomId, sessionId }, explicit);
  }, [activeRoomId, sessionId, composing, explicitPicks]);
  useEffect(() => {
    // A bare /chat entry carries no command for the reconciler; Back onto one
    // shows Messages instead of leaving the last panel on screen.
    const onPop = () => { if (parseCommand(window.location.search) === null) activateMessagesPanel(); };
    window.addEventListener("popstate", onPop);
    return () => {
      window.removeEventListener("popstate", onPop);
      resetChatView();
    };
  }, []);
  return null;
}
