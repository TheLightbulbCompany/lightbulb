"use client";

import { MessagingWorkspaceProvider, useMessagingWorkspace } from "./MessagingWorkspaceContext";
import { RoomDirectory } from "./RoomDirectory";
import { RoomPanel } from "./RoomPanel";
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
    onCreate={(creation) => {
      onOpenRoom?.();
      return directoryProps.onCreate?.(creation);
    }}
  />;
}

export function MessagingConversation() {
  const { roomProps } = useMessagingWorkspace();
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
