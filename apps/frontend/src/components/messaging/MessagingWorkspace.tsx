"use client";

import { useEffect, useRef } from "react";
import { MessagingWorkspaceProvider, useMessagingWorkspace } from "./MessagingWorkspaceContext";
import { RoomDirectory } from "./RoomDirectory";
import { RoomPanel } from "./RoomPanel";
import { SessionFocus } from "./SessionFocus";
import type { DirectoryRoom } from "./types";
import "./messaging.css";

export function MessagingDirectory({ onOpenRoom }: { onOpenRoom?: (room?: DirectoryRoom) => void }) {
  const { directoryProps } = useMessagingWorkspace();
  return <RoomDirectory
    {...directoryProps}
    onSelectRoom={(roomId) => {
      directoryProps.onSelectRoom(roomId);
      const room = directoryProps.rooms.find((candidate) => candidate.roomId === roomId);
      if (room) onOpenRoom?.(room);
    }}
    onSelectParticipant={(participant) => {
      onOpenRoom?.();
      return directoryProps.onSelectParticipant(participant);
    }}
    onSelectSession={(sessionId) => {
      onOpenRoom?.();
      directoryProps.onSelectSession?.(sessionId);
    }}
    onCreate={(creation) => {
      onOpenRoom?.();
      return directoryProps.onCreate?.(creation);
    }}
  />;
}

export function MessagingConversation() {
  const { roomProps, sessionFocus, closeSession, sessionSource, scope, directoryProps } = useMessagingWorkspace();
  if (sessionFocus) {
    return <SessionFocus key={scope} focus={sessionFocus} source={sessionSource} onClose={closeSession}
      conversation={roomProps ? <RoomPanel {...roomProps} /> : null} scope={scope} />;
  }
  return roomProps
    ? <RoomPanel {...roomProps} />
    : <section className="room-panel room-blocked" aria-label="No conversation selected">
        <p role="status">{directoryProps.state === "ready" && directoryProps.rooms.length === 0
          // No rooms at all only happens before setup seeds #general.
          ? "Your team is getting ready — #general will open here."
          : "Choose or create a conversation."}</p>
      </section>;
}

export function MessagingWorkspace(props: {
  initialAgentId?: string | null;
  defaultAgentId?: string | null;
  subscribeToRoom?: boolean;
  roomId?: string | null;
  messageId?: string | null;
}) {
  return <MessagingWorkspaceProvider initialAgentId={props.roomId ? null : props.initialAgentId}
    defaultAgentId={props.roomId ? null : props.defaultAgentId} subscribeToRoom={props.subscribeToRoom}>
    <RoomLink roomId={props.roomId} messageId={props.messageId} />
    <div className="messaging-workspace">
      <MessagingDirectory />
      <MessagingConversation />
    </div>
  </MessagingWorkspaceProvider>;
}

function RoomLink({ roomId, messageId }: { roomId?: string | null; messageId?: string | null }) {
  const { openMessage, scope } = useMessagingWorkspace();
  const consumed = useRef<string | null>(null);
  useEffect(() => {
    const key = roomId ? JSON.stringify([scope, roomId, messageId ?? null]) : null;
    if (key && key !== consumed.current) openMessage(roomId!, messageId ?? null);
    consumed.current = key;
  }, [roomId, messageId, openMessage, scope]);
  return null;
}
