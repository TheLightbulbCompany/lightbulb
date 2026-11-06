"use client";

import { useEffect, useRef } from "react";
import { useMessagingWorkspace } from "@/components/messaging/MessagingWorkspaceContext";
import { setCurrentEntryNamer } from "@/lib/shellNav";
import { currentChatViewSearch, recordChatView, resetChatView, setUnrecordedView } from "./chatHistory";
import { parseCommand } from "./urlChannel";
import { activateMessagesPanel, leavePaneFor, useWorkspaceState } from "./workspaceController";

/** Puts the person's room, DM and session picks into browser history, and closes a pane item that is not the new
 *  room's (desktop shell only). */
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
  // Into Chat from another place, or to another room: a pane item that is not this room's closes (W17-9). The room's
  // first resolution on load is not a move (a reloaded pane stays).
  const { place } = useWorkspaceState();
  const seen = useRef({ roomId: activeRoomId, place });
  useEffect(() => {
    const was = seen.current;
    seen.current = { roomId: activeRoomId, place };
    if (place !== "chat" || !was.roomId || (was.roomId === activeRoomId && was.place === "chat")) return;
    leavePaneFor(activeRoomId);
  }, [activeRoomId, place]);
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
