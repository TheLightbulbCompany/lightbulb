"use client";

import { AllSessionsView } from "./AllSessionsView";
import { useMessagingWorkspace } from "./MessagingWorkspaceContext";
import { NewSessionButton, NewSessionView } from "./NewSessionButton";
import { RoomDirectory } from "./RoomDirectory";
import { RoomPanel } from "./RoomPanel";
import { SessionFocus } from "./SessionFocus";
import type { DirectoryRoom } from "./types";
import "./messaging.css";

export function MessagingDirectory({ onOpenRoom }: { onOpenRoom?: (room?: DirectoryRoom) => void }) {
  const { directoryProps, openNewSession } = useMessagingWorkspace();
  return <RoomDirectory
    {...directoryProps}
    headerActions={<NewSessionButton onOpen={() => { onOpenRoom?.(); openNewSession(); }} />}
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
    onOpenAllSessions={() => {
      onOpenRoom?.();
      directoryProps.onOpenAllSessions?.();
    }}
    onCreate={(creation) => {
      onOpenRoom?.();
      return directoryProps.onCreate?.(creation);
    }}
  />;
}

export function MessagingConversation() {
  const { roomProps, sessionFocus, openSession, closeSession, setSessionChat, setSessionChatRoomId, sessionSource, resolveSessionOwner, workspaceSessions, scope, directoryProps } = useMessagingWorkspace();
  if (sessionFocus?.all) {
    return <AllSessionsView key={scope} entries={directoryProps.sessions ?? []} loading={workspaceSessions.loading}
      onOpenSession={openSession} onClose={closeSession} onRename={directoryProps.onRenameSession} />;
  }
  if (sessionFocus?.sessionId === null) {
    return <NewSessionView key={scope} sessions={workspaceSessions.sessions} onEvent={sessionSource.onEvent}
      onOpenSession={openSession} onClose={closeSession} />;
  }
  if (sessionFocus) {
    return <SessionFocus key={scope} focus={{ sessionId: sessionFocus.sessionId }} entries={directoryProps.sessions ?? []} loading={workspaceSessions.loading}
      hintRevision={workspaceSessions.hintRevision} client={sessionSource.client} onEvent={sessionSource.onEvent} resolveSessionOwner={resolveSessionOwner}
      onAccessLost={workspaceSessions.refresh} onClose={closeSession} participants={directoryProps.participants}
      chatOpen={!!sessionFocus.chat} onChatOpenChange={setSessionChat} onChatRoom={setSessionChatRoomId}
      onMessageOwner={(id, displayName) => void directoryProps.onSelectParticipant(
        directoryProps.participants.find((p) => p.kind === "human" && p.id === id) ?? { kind: "human", id, displayName, mentionHandle: null })} />;
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
