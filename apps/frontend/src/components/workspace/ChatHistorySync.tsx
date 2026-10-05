"use client";

import { useEffect, useRef } from "react";
import { useMessagingWorkspace } from "@/components/messaging/MessagingWorkspaceContext";
import { setCurrentEntryNamer } from "@/lib/shellNav";
import { currentChatViewSearch, recordChatView, resetChatView, setUnrecordedView } from "./chatHistory";
import { parseCommand } from "./urlChannel";
import { activateMessagesPanel } from "./workspaceController";

/** Puts the person's room, DM and session picks into browser history (desktop shell only). */
export function ChatHistorySync(): null {
  const { activeRoomId, sessionFocus, explicitPickCount } = useMessagingWorkspace();
  const seenPicks = useRef(explicitPickCount);
  const sessionId = sessionFocus?.sessionId ?? null;
  const prevView = useRef({ roomId: activeRoomId, sessionId });
  // The new-session composer and the all-sessions list are not places to go back to.
  const composing = !!sessionFocus && !sessionFocus.sessionId;
  useEffect(() => {
    setUnrecordedView(composing);
    if (composing) return;
    const was = prevView.current;
    prevView.current = { roomId: activeRoomId, sessionId };
    // This child effect runs before the provider's, which clears the session when
    // the room moves. Wait for that commit; the pick stays unseen until then.
    if (sessionId && sessionId === was.sessionId && was.roomId && activeRoomId !== was.roomId) return;
    // Only a pick counts, so a held session link that lands later is not one; a
    // pick that changes nothing (the open room, re-picked from a panel) still is.
    const explicit = explicitPickCount !== seenPicks.current;
    seenPicks.current = explicitPickCount;
    recordChatView({ roomId: activeRoomId, sessionId }, explicit);
  }, [activeRoomId, sessionId, composing, explicitPickCount]);
  useEffect(() => {
    // A bare /chat entry carries no command for the reconciler; Back onto one
    // shows Messages instead of leaving the last panel on screen.
    const onPop = () => { if (parseCommand(window.location.search) === null) activateMessagesPanel(); };
    window.addEventListener("popstate", onPop);
    setCurrentEntryNamer(currentChatViewSearch);
    return () => {
      window.removeEventListener("popstate", onPop);
      setCurrentEntryNamer(null);
      resetChatView();
    };
  }, []);
  return null;
}
