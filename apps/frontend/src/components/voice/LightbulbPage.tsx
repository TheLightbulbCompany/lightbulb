"use client";

// The Lightbulb place (founder, 8 Nov 2026): hold to talk on top, and under it the member's DM with the assistant
// exactly as /chat rendered it (composer, messages, tasks with statuses, Workers' results and cards). The room's
// side column (a thread, its tasks, room details) renders inline: no workspace shell is mounted, so useThreadPane
// keeps it in the room. What the room opens in the shell's right pane (a file or result, a task, an automation) opens in
// the same RightPane beside the room here: openPanel keeps the pane slot without a shell (workspaceController).
// The room's side column (Tasks, Files, Scheduled, Details) draws its title and ✕ with PaneHeader, whose styles live
// in workspace.css; no workspace shell is mounted here to load them, so the header fell apart (title over the ✕).
import "@/components/workspace/workspace.css";
import { useEffect, useRef } from "react";
import { RightPane } from "@/components/workspace/RightPane";
import { closePane, useWorkspaceState } from "@/components/workspace/workspaceController";
import { TalkOrb } from "./TalkOrb";
import { LIGHTBULB } from "./data";
import { useAssistantDm } from "@/components/huddle/useAssistantDm";
import { MessagingWorkspaceProvider, useMessagingWorkspace } from "@/components/messaging/MessagingWorkspaceContext";
import { MessagingConversation } from "@/components/messaging/MessagingWorkspace";
import { InvitePeopleDialog } from "@/components/workspace/InvitePeopleDialog";

export function LightbulbPage() {
  return <div className="vx-lightbulb">
    <div className="vx-page"><TalkOrb name={LIGHTBULB} /></div>
    <MessagingWorkspaceProvider>
      <div className="workspace-body">
        <div className="workspace-view"><AssistantRoom /></div>
        <Pane />
      </div>
      {/* The welcome's "Invite people" (RoomPanel) and the onboarding band open this; WorkspaceShell mounted it before. */}
      <InvitePeopleDialog />
    </MessagingWorkspaceProvider>
  </div>;
}

/** The right pane, one thing at a time, closed when the page goes (the shell's own slots start empty). */
function Pane() {
  const { pane } = useWorkspaceState();
  useEffect(() => closePane, []);
  return pane ? <RightPane key={pane.panelId} pane={pane} fullScreen={false} /> : null;
}

/** The room the orb talks in (useAssistantDm), opened once it is known. No DM at all: the provider's own pick (a
 *  fresh workspace auto-opens its first-run room or #general, as /chat did). */
function AssistantRoom() {
  const dm = useAssistantDm();
  const { activeRoomId, openMessage } = useMessagingWorkspace();
  const roomId = dm?.roomId;
  const opened = useRef<string | null>(null);
  useEffect(() => {
    if (!roomId || opened.current === roomId) return;
    opened.current = roomId;
    openMessage(roomId, null);
  }, [roomId, openMessage]);
  if (dm === undefined || (roomId && activeRoomId !== roomId)) return <p className="vx-empty" role="status">Loading chat…</p>;
  // The orb's pre-connect is a real Live in this room, so the header's Live strip and Go live would be a second control.
  // The member's own DM with the assistant: no Members tab, Add people or invites (RoomPanel).
  return <MessagingConversation voiceControls={false} oneToOne />;
}
