"use client";

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
  const { roomProps, sessionFocus, closeSession, currentUserId, resourceSource } = useMessagingWorkspace();
  if (sessionFocus && resourceSource) {
    return <SessionFocus focus={sessionFocus} source={resourceSource} own={sessionFocus.ownerUserId === currentUserId} onClose={closeSession}
      conversation={roomProps ? <RoomPanel {...roomProps} /> : null} />;
  }
  return roomProps
    ? <RoomPanel {...roomProps} />
    : <section className="room-panel room-blocked" aria-label="No conversation selected">
        <p role="status">Choose or create a conversation.</p>
      </section>;
}

export function MessagingWorkspace(props: {
  initialAgentId?: string | null;
  subscribeToRoom?: boolean;
}) {
  return <MessagingWorkspaceProvider {...props}>
    <div className="messaging-workspace">
      <MessagingDirectory />
      <MessagingConversation />
    </div>
  </MessagingWorkspaceProvider>;
}
