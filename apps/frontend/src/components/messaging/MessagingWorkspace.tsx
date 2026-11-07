"use client";

import { useMessagingWorkspace } from "./MessagingWorkspaceContext";
import { RoomDirectory } from "./RoomDirectory";
import { RoomPanel } from "./RoomPanel";
import { SessionFocus } from "./SessionFocus";
import type { DirectoryRoom } from "./types";
import "./messaging.css";

export function MessagingDirectory({ onOpenRoom, onNavigate }: { onOpenRoom?: (room?: DirectoryRoom) => void; onNavigate?: () => void }) {
  const { directoryProps } = useMessagingWorkspace();
  return <RoomDirectory
    {...directoryProps}
    onNavigate={onNavigate}
    onSelectRoom={(roomId) => {
      directoryProps.onSelectRoom(roomId);
      const room = directoryProps.rooms.find((candidate) => candidate.roomId === roomId);
      if (room) onOpenRoom?.(room);
    }}
    onSelectParticipant={(participant) => {
      onOpenRoom?.();
      return directoryProps.onSelectParticipant(participant);
    }}
    onAddPeople={directoryProps.onAddPeople && ((roomId) => {
      directoryProps.onAddPeople!(roomId);
      const room = directoryProps.rooms.find((candidate) => candidate.roomId === roomId);
      if (room) onOpenRoom?.(room);
    })}
    onCreate={(creation) => {
      onOpenRoom?.();
      return directoryProps.onCreate?.(creation);
    }}
  />;
}

export function MessagingConversation() {
  const { roomProps, sessionFocus, closeSession, setSessionChat, setSessionChatRoomId, sessionSource, resolveSessionOwner, workspaceSessions, scope, directoryProps } = useMessagingWorkspace();
  // The session viewer, reachable by link (?session=); the Sessions list and New session are gone (voice pivot).
  if (sessionFocus?.sessionId) {
    return <SessionFocus key={scope} focus={{ sessionId: sessionFocus.sessionId, seq: sessionFocus.seq }} entries={directoryProps.sessions ?? []}
      hintRevision={workspaceSessions.hintRevision} client={sessionSource.client} onEvent={sessionSource.onEvent} resolveSessionOwner={resolveSessionOwner}
      onAccessLost={workspaceSessions.refresh} onClose={closeSession} participants={directoryProps.participants} onRemove={directoryProps.onRemoveSession}
      chatOpen={!!sessionFocus.chat} onChatOpenChange={setSessionChat} onChatRoom={setSessionChatRoomId}
      onMessageOwner={(id, displayName) => void directoryProps.onSelectParticipant(
        directoryProps.participants.find((p) => p.kind === "human" && p.id === id) ?? { kind: "human", id, displayName, mentionHandle: null })} />;
  }
  return roomProps
    ? <RoomPanel {...roomProps} />
    : <section className="room-panel room-blocked" aria-label="No chat selected">
        <p role="status">{directoryProps.state === "ready" && directoryProps.rooms.length === 0
          // No rooms at all only happens before setup seeds #general.
          ? "Your team is getting ready — #general will open here."
          : "Choose a chat."}</p>
      </section>;
}
