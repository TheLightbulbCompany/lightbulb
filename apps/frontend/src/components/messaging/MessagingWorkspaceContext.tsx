"use client";

import { createContext, useContext, useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useAuth } from "@clerk/nextjs";
import { resolveTypedMentions, runView, type CreateMessageRequest, type ListenMode, type MemberRef, type RoomSummary } from "@lightbulb/shared";
import { useRooms } from "@/hooks/useRooms";
import { useAgents } from "@/hooks/useAgents";
import { useRosterEntries } from "@/components/agents/useRosterEntries";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import type { CompanyAgent, CompanyMember } from "@/components/teams/shared/types";
import { activateMessagesPanel, openPanel } from "@/components/workspace/workspaceController";
import { MessageNotifications } from "@/components/notifications/MessageNotifications";
import { UnreadTitleBadge } from "@/components/notifications/UnreadTitleBadge";
import { hasBlockingShortcutDialog } from "@/components/teams/shared/lib/keyboardShortcuts";
import { clearDraft, draftKey, listDraftRoomIds, readDraft, writeDraft } from "./drafts";
import { adjacentRoomId, nextUnreadRoomId } from "./roomNavigation";
import { usePresence } from "./usePresence";
import { ShortcutSheet } from "./ShortcutSheet";
import type { RoomComposerProps } from "./RoomComposer";
import type { RoomDirectoryProps, ConversationCreation } from "./RoomDirectory";
import type { RoomPanelProps } from "./RoomPanel";
import type { DirectoryRoom, DirectoryParticipant, RoomParticipant } from "./types";

export interface MessagingWorkspaceValue {
  activeRoomId: string | null;
  directoryProps: RoomDirectoryProps;
  roomProps: RoomPanelProps | null;
  selectAgentDm: (agentId: string) => Promise<string | null>;
  openMessage: (roomId: string, messageId: string | null) => void;
  openSession: (roomId: string, sessionId: string) => void;
}

const MessagingWorkspaceContext = createContext<MessagingWorkspaceValue | null>(null);

export function useMessagingWorkspace(): MessagingWorkspaceValue {
  const value = useContext(MessagingWorkspaceContext);
  if (!value) throw new Error("useMessagingWorkspace must be used within MessagingWorkspaceProvider");
  return value;
}

/** Null outside a MessagingWorkspaceProvider — the Teams command palette is
 *  also mounted in InlineTeamsPanel, where no messaging provider exists. */
export function useMessagingWorkspaceOptional(): MessagingWorkspaceValue | null {
  return useContext(MessagingWorkspaceContext);
}

/**
 * Window-level shortcuts for room navigation (⌥↑/⌥↓ adjacent room, ⌥⇧↑/⌥⇧↓
 * next/previous unread — both wrapping via roomNavigation.ts, the same rule
 * RoomDirectory's "next unread" button uses) and the shortcut sheet (⌘/,
 * toggled via `onShowShortcuts`).
 *
 * The room-navigation (⌥) branches are ignored while a Radix dialog/menu is
 * open or an IME composition is in progress. They deliberately still fire
 * from inside the composer — that is where the owner's hands are, and the
 * combos are modified, so they are not what anyone types into a message.
 * `defaultPrevented` covers everything the composer or a typeahead already
 * handled (e.g. its own plain-ArrowUp "edit last message"). `⌘/` carries its
 * own dialog guard rather than the shared one below: it must still toggle
 * the shortcut sheet closed while that very sheet (its own `role="dialog"`)
 * is open, but must not stack a sheet over someone else's dialog — the ⌘K
 * palette.
 */
function useMessagingShortcuts(
  rooms: readonly RoomSummary[],
  activeRoomId: string | null,
  selectRoom: (roomId: string) => void,
  onShowShortcuts: () => void,
  shortcutsOpen: boolean,
): void {
  useEffect(() => {
    const handler = (event: KeyboardEvent) => {
      if (event.defaultPrevented || event.isComposing || event.keyCode === 229) return;
      if ((event.metaKey || event.ctrlKey) && !event.altKey && !event.shiftKey && event.key === "/") {
        if (!shortcutsOpen && hasBlockingShortcutDialog()) return;
        event.preventDefault();
        onShowShortcuts();
        return;
      }
      if (hasBlockingShortcutDialog() || document.querySelector("[role='menu']")) return;
      if (!event.altKey || event.metaKey || event.ctrlKey) return;
      if (event.key !== "ArrowUp" && event.key !== "ArrowDown") return;
      const direction = event.key === "ArrowDown" ? 1 : -1;
      const next = event.shiftKey
        ? nextUnreadRoomId(rooms, activeRoomId, direction)
        : adjacentRoomId(rooms, activeRoomId, direction);
      // Also a no-op when the only unread room is the one already open —
      // adjacentRoomId already excludes this case itself, but
      // nextUnreadRoomId (unlike RoomDirectory's own "next unread" button)
      // must not reselect the active room from this window-level shortcut.
      if (!next || next === activeRoomId) return;
      event.preventDefault();
      selectRoom(next);
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [rooms, activeRoomId, selectRoom, onShowShortcuts, shortcutsOpen]);
}

export function MessagingWorkspaceProvider({
  children,
  initialAgentId,
  subscribeToRoom = true,
}: {
  children: ReactNode;
  initialAgentId?: string | null;
  subscribeToRoom?: boolean;
}) {
  const { userId, orgId } = useAuth();
  const scope = orgId ?? userId ?? "signed-out";
  const rooms = useRooms(initialAgentId, subscribeToRoom);
  const setRoomTyping = rooms.setTyping;
  const sendRoomMessage = rooms.sendMessage;
  const teams = useTeamsApi();
  const { agents } = useAgents();
  const roster = useRosterEntries(agents);
  const activityByAgentId = useMemo(() => new Map(roster.map((entry) => [entry.agent.id, entry.state])), [roster]);
  const { data: agentRoster } = teams.read<{ agents?: CompanyAgent[] }>("/agents");
  const { data: humanRoster } = teams.read<{ members?: CompanyMember[] }>("/members");
  const [draft, setDraft] = useState("");
  const [clientMessageId, setClientMessageId] = useState(() => crypto.randomUUID());
  const [threadId, setThreadId] = useState<string | null>(null);
  const [notificationTarget, setNotificationTarget] = useState<{ roomId: string; threadId: string | null } | null>(null);
  useEffect(() => {
    if (!notificationTarget || notificationTarget.roomId !== rooms.activeRoomId) return;
    setThreadId(notificationTarget.threadId);
    setNotificationTarget(null);
  }, [notificationTarget, rooms.activeRoomId]);
  const [threadDraft, setThreadDraft] = useState("");
  const [threadClientMessageId, setThreadClientMessageId] = useState(() => crypto.randomUUID());
  const clientMessageIdRef = useRef(clientMessageId);
  const threadClientMessageIdRef = useRef(threadClientMessageId);
  clientMessageIdRef.current = clientMessageId;
  threadClientMessageIdRef.current = threadClientMessageId;
  const [creationBusy, setCreationBusy] = useState(false);
  const [creationError, setCreationError] = useState<string>();
  // A failed DM resolve-or-create (rooms.selectRoomError) otherwise vanishes:
  // the caller rethrows but nobody renders it. Dismissal is local — the hook
  // itself clears the error on the next successful select or create.
  const [selectErrorDismissed, setSelectErrorDismissed] = useState<string | null>(null);
  const selectRoomError = rooms.selectRoomError && rooms.selectRoomError !== selectErrorDismissed
    ? rooms.selectRoomError
    : null;
  const [shortcutsOpen, setShortcutsOpen] = useState(false);
  const toggleShortcuts = useCallback(() => setShortcutsOpen((open) => !open), []);
  useMessagingShortcuts(rooms.rooms, rooms.activeRoomId, rooms.selectRoom, toggleShortcuts, shortcutsOpen);
  const [draftRoomId, setDraftRoomId] = useState(rooms.activeRoomId);
  const [draftThreadId, setDraftThreadId] = useState<string | null>(null);
  const typingActive = useRef(false);
  const typingTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Reset before the new room's composer consumes a pending draft/focus
  // handoff — restoring, not clearing, so a half-typed message survives a
  // room switch (spec: per-room drafts). There is no separate `mentions`
  // state to restore: the composer derives mentions purely from the typed
  // text (resolveTypedMentions), so restoring `text` is enough — the
  // structured mentions come back on their own once the text re-renders.
  if (draftRoomId !== rooms.activeRoomId) {
    setDraftRoomId(rooms.activeRoomId);
    setDraft(rooms.activeRoomId ? readDraft(draftKey(scope, rooms.activeRoomId, null)).text : "");
    setClientMessageId(crypto.randomUUID());
    setThreadId(null);
    setDraftThreadId(null);
    setThreadDraft("");
    setThreadClientMessageId(crypto.randomUUID());
  } else if (draftThreadId !== threadId) {
    setDraftThreadId(threadId);
    setThreadDraft(rooms.activeRoomId && threadId ? readDraft(draftKey(scope, rooms.activeRoomId, threadId)).text : "");
    setThreadClientMessageId(crypto.randomUUID());
  }

  useEffect(() => () => {
    if (typingTimer.current) clearTimeout(typingTimer.current);
    if (typingActive.current) void setRoomTyping(false);
  }, [setRoomTyping]);

  const online = usePresence(rooms.httpReady, scope);
  const participants: RoomParticipant[] = useMemo(() => rooms.activeRoom?.members.map((member) => member.kind === "agent"
    ? { ...member, agentActivity: activityByAgentId.get(member.id) }
    : { ...member, online: online.has(member.id) }) ?? [], [rooms.activeRoom, activityByAgentId, online]);
  const directoryRooms: DirectoryRoom[] = useMemo(() => rooms.rooms.map((room) => ({
    ...room,
    participants: (rooms.directoryRoomDetails.get(room.roomId)?.members ?? [])
      .filter((member) => member.kind !== "human" || member.id !== userId)
      .map((member) => member.kind === "agent"
        ? { ...member, agentActivity: activityByAgentId.get(member.id) }
        : { ...member, online: online.has(member.id) }),
  })), [rooms.rooms, rooms.directoryRoomDetails, activityByAgentId, userId, online]);
  const directoryParticipants: DirectoryParticipant[] = useMemo(() => {
    const people = (humanRoster?.members ?? [])
      .filter((member) => member.userId !== userId)
      .map((member) => ({
      kind: "human" as const,
      id: member.userId,
      displayName: member.name || member.email || "Workspace member",
      mentionHandle: null,
      online: online.has(member.userId),
    }));
    const agents = (agentRoster?.agents ?? []).map((agent) => ({
      kind: "agent" as const,
      id: agent.id,
      displayName: agent.name,
      mentionHandle: rooms.activeRoom?.members.find((member) => member.kind === "agent" && member.id === agent.id)?.mentionHandle ?? null,
    }));
    return [...people, ...agents];
  }, [humanRoster, agentRoster, rooms.activeRoom, userId, online]);

  const handleTyping = useCallback((isTyping: boolean) => {
    if (typingTimer.current) clearTimeout(typingTimer.current);
    if (!isTyping) {
      if (typingActive.current) void setRoomTyping(false);
      typingActive.current = false;
      return;
    }
    if (!typingActive.current) void setRoomTyping(true);
    typingActive.current = true;
    typingTimer.current = setTimeout(() => {
      typingActive.current = false;
      void setRoomTyping(false);
    }, 3_000);
  }, [setRoomTyping]);

  // Persist as the owner types — recomputed structured mentions come from
  // the same resolveTypedMentions the send path uses, so a restored draft's
  // mentions are exactly what would be dispatched if sent as-is.
  useEffect(() => {
    if (!rooms.activeRoomId) return;
    writeDraft(draftKey(scope, rooms.activeRoomId, null), { text: draft, mentions: resolveTypedMentions(draft, participants) });
  }, [scope, rooms.activeRoomId, draft, participants]);

  useEffect(() => {
    if (!rooms.activeRoomId || !threadId) return;
    writeDraft(draftKey(scope, rooms.activeRoomId, threadId), { text: threadDraft, mentions: resolveTypedMentions(threadDraft, participants) });
  }, [scope, rooms.activeRoomId, threadId, threadDraft, participants]);

  const send = useCallback(async (request: CreateMessageRequest, thread = false) => {
    // Read before the await: this is the room the message was sent FROM, even
    // if the owner switches rooms while it is in flight.
    const roomId = rooms.activeRoomId;
    await sendRoomMessage(request);
    // Clearing the SENDING room's stored row is always correct once the send
    // succeeded — so it happens before the attempt-id guards, which a room
    // switch mid-send trips and which only protect the NEW room's in-memory
    // draft state below.
    if (thread) {
      if (roomId && request.threadRootMessageId) clearDraft(draftKey(scope, roomId, request.threadRootMessageId));
      if (threadClientMessageIdRef.current !== request.clientMessageId) return;
      setThreadDraft("");
      setThreadClientMessageId(crypto.randomUUID());
    } else {
      if (roomId) clearDraft(draftKey(scope, roomId, null));
      if (clientMessageIdRef.current !== request.clientMessageId) return;
      setDraft("");
      setClientMessageId(crypto.randomUUID());
    }
  }, [sendRoomMessage, rooms.activeRoomId, scope]);

  // Read every render: localStorage is the source, so no prop dependency
  // could describe when this changes.
  const draftRoomIds = listDraftRoomIds(scope);

  const agentCounterparts = participants.filter((member) => member.kind === "agent");
  const runningAgentIds = rooms.runs.filter((run) => run.state === "running").map((run) => run.agentId);
  const composer: RoomComposerProps = {
    focusAgentId: rooms.activeRoom?.kind === "dm" && agentCounterparts.length === 1 ? agentCounterparts[0].id : undefined,
    text: draft,
    onTextChange: setDraft,
    members: participants,
    clientMessageId,
    sendState: rooms.sendState(clientMessageId),
    onSubmit: (request) => send(request),
    onRetry: (request) => send(request),
    onReconcile: (id) => rooms.reconcileSend(id),
    disabled: !rooms.httpReady,
    onTypingChange: handleTyping,
    onUploadAttachment: rooms.uploadAttachment,
    roomKind: rooms.activeRoom?.kind,
    runningAgentIds,
  };

  const threadComposer: { parentMessageId: string; composer: RoomComposerProps } | undefined = threadId ? {
    parentMessageId: threadId,
    composer: {
      text: threadDraft,
      onTextChange: setThreadDraft,
      members: participants,
      clientMessageId: threadClientMessageId,
      sendState: rooms.sendState(threadClientMessageId),
      onSubmit: (request: CreateMessageRequest) => send(request, true),
      onRetry: (request: CreateMessageRequest) => send(request, true),
      onReconcile: (id: string) => rooms.reconcileSend(id),
      replyToMessageId: threadId,
      threadRootMessageId: threadId,
      disabled: !rooms.httpReady,
      onTypingChange: handleTyping,
      onUploadAttachment: rooms.uploadAttachment,
      roomKind: rooms.activeRoom?.kind,
      runningAgentIds,
    },
  } : undefined;

  async function createRoom(creation: ConversationCreation) {
    setCreationBusy(true);
    setCreationError(undefined);
    try {
      await rooms.createRoom({ clientRoomId: crypto.randomUUID(), ...creation });
      return true;
    } catch (error) {
      setCreationError(error instanceof Error ? error.message : "Conversation could not be created.");
      return false;
    } finally {
      setCreationBusy(false);
    }
  }

  async function selectParticipant(participant: DirectoryParticipant) {
    setCreationBusy(true);
    setCreationError(undefined);
    try {
      await rooms.selectDm(participant);
    } catch (error) {
      setCreationError(error instanceof Error ? error.message : "Conversation could not be opened.");
    } finally {
      setCreationBusy(false);
    }
  }

  async function changeListenMode(agentId: string, listenMode: ListenMode) {
    const member = participants.find((candidate) => candidate.kind === "agent" && candidate.id === agentId);
    if (!member) return;
    await rooms.upsertMember({ kind: "agent", id: agentId, role: "member", listenMode });
  }

  async function addMember(member: MemberRef) {
    await rooms.upsertMember({
      kind: member.kind,
      id: member.id,
      role: "member",
      listenMode: member.kind === "agent" ? "mentions" : null,
    });
  }

  // Permalink target: opened via openMessage (a room permalink), cleared 3s
  // after opening rather than persisting indefinitely once the highlight has
  // had time to land.
  const [target, setTarget] = useState<{ roomId: string; messageId: string | null } | null>(null);
  const [sessionTarget, setSessionTarget] = useState<{ roomId: string; sessionId: string; nonce: number } | null>(null);
  const sessionNonce = useRef(0);
  const selectRoom = rooms.selectRoom;
  const openSession = useCallback((roomId: string, sessionId: string) => {
    selectRoom(roomId);
    setThreadId(null);
    setTarget(null);
    setSessionTarget({ roomId, sessionId, nonce: ++sessionNonce.current });
  }, [selectRoom]);
  const openMessage = useCallback((roomId: string, messageId: string | null) => {
    setSessionTarget(null);
    selectRoom(roomId);
    setTarget({ roomId, messageId });
  }, [selectRoom]);
  const previousSelectedRoom = useRef(rooms.activeRoomId);
  useEffect(() => {
    if (rooms.accessRemoved || (previousSelectedRoom.current !== rooms.activeRoomId && sessionTarget?.roomId !== rooms.activeRoomId)) setSessionTarget(null);
    previousSelectedRoom.current = rooms.activeRoomId;
  }, [rooms.activeRoomId, rooms.accessRemoved, sessionTarget]);
  useEffect(() => {
    if (!target?.messageId) return;
    const timer = setTimeout(() => setTarget(null), 3_000);
    return () => clearTimeout(timer);
  }, [target]);

  const readSeqAtOpen = useRef<{ roomId: string; seq: number | null } | null>(null);
  if (rooms.activeRoom && readSeqAtOpen.current?.roomId !== rooms.activeRoom.roomId) readSeqAtOpen.current = { roomId: rooms.activeRoom.roomId, seq: rooms.activeRoom.lastReadSeq ?? null };

  const directoryState = rooms.roomsLoading || rooms.directoryLoading ? "loading" : rooms.roomsError || rooms.directoryError ? "error" : "ready";
  const roomState = rooms.roomLoading ? "loading" : rooms.accessRemoved ? "access-removed" : rooms.roomError ? "unavailable" : rooms.liveConnected ? "ready" : "reconnecting";
  const pending = rooms.optimisticMessages.filter((message) => message.clientMessageId === clientMessageId || message.clientMessageId === threadClientMessageId);
  const viewerRole = rooms.activeRoom?.members.find((member) => member.kind === "human" && member.id === userId)?.role ?? "";
  const canManageResponses = ["owner", "admin"].includes(viewerRole);

  const directoryProps: RoomDirectoryProps = {
    ownerScopeId: scope,
    rooms: directoryRooms,
    sections: rooms.sections,
    participants: directoryParticipants,
    state: directoryState,
    currentRoomId: rooms.activeRoomId ?? undefined,
    onSelectRoom: rooms.selectRoom,
    onSelectParticipant: selectParticipant,
    onMarkUnread: rooms.markUnread,
    onSetPrefs: rooms.setRoomPrefs,
    onSaveSections: rooms.saveSections,
    onLeaveRoom: rooms.leaveRoom,
    onCreate: createRoom,
    creationBusy,
    creationError,
    onRetry: () => void rooms.refreshRooms(),
    draftRoomIds,
  };
  const roomProps: RoomPanelProps | null = rooms.activeRoomId ? {
    ownerScopeId: scope,
    currentUserId: userId ?? "",
    roomId: rooms.activeRoomId,
    room: rooms.activeRoom,
    readSeqAtOpen: readSeqAtOpen.current?.roomId === rooms.activeRoomId ? readSeqAtOpen.current.seq : null,
    targetMessageId: target?.roomId === rooms.activeRoomId ? target.messageId : null,
    targetSession: sessionTarget?.roomId === rooms.activeRoomId ? sessionTarget : null,
    state: roomState,
    httpReady: rooms.httpReady,
    liveConnected: rooms.liveConnected,
    resourceSource: subscribeToRoom ? rooms.resourceSource : undefined,
    participants,
    messages: rooms.messages,
    historyLoading: rooms.historyLoading,
    historyComplete: rooms.historyComplete,
    historyError: rooms.historyError,
    historyErrorMessage: rooms.historyErrorMessage,
    onLoadHistory: rooms.loadHistory,
    pendingMessages: pending,
    typingMembers: rooms.typingMembers.filter((member) => member.userId !== userId),
    runs: rooms.runs.map(runView),
    composer,
    threadComposer,
    onSelectThread: setThreadId,
    selectedThreadId: threadId,
    onSelectParticipant: async (participant) => { await rooms.selectDm(participant); },
    onRetry: () => void rooms.refreshRoom(),
    ...(canManageResponses ? { onListenModeChange: (agentId: string, mode: ListenMode) => void changeListenMode(agentId, mode) } : {}),
    onSetReaction: rooms.setReaction,
    onUpdateMessage: rooms.updateMessage,
    pinnedMessageIds: rooms.activeRoom?.pinnedMessageIds ?? [],
    onSetPin: rooms.setPin,
    onMarkUnreadFrom: async (fromSeq: number) => { if (rooms.activeRoomId) await rooms.markUnread(rooms.activeRoomId, fromSeq); },
    onSetPrefs: async (prefs) => { if (rooms.activeRoomId) await rooms.setRoomPrefs(rooms.activeRoomId, prefs); },
    onLeaveRoom: rooms.activeRoom?.kind === "channel" && viewerRole !== "owner"
      ? async () => { if (rooms.activeRoomId) await rooms.leaveRoom(rooms.activeRoomId); } : undefined,
    ...(rooms.activeRoom && rooms.activeRoom.kind !== "dm" && ["owner", "admin"].includes(viewerRole) ? {
      onRenameRoom: async (name: string) => { await rooms.updateRoom({ name }); },
      onSetTopic: async (topic: string | null) => { await rooms.updateRoom({ topic }); },
      ...(rooms.activeRoom.kind === "channel" ? {
        addMemberCandidates: directoryParticipants.filter((candidate) => !rooms.activeRoom!.members.some(
          (member) => member.kind === candidate.kind && member.id === candidate.id,
        )),
        onAddMember: addMember,
        onRemoveMember: rooms.removeMember,
      } : {}),
    } : {}),
    onAbortRun: (runId) => void rooms.abortRun(runId),
    onSteerRun: (runId, text) => void rooms.steerRun(runId, text),
    onLinkToWork: () => openPanel("teams-issues", {}, { pushUrl: true }),
    onDownloadAttachment: rooms.downloadAttachment,
  } : null;

  // Muted rooms never badge anywhere: not the sidebar, not the browser-tab title.
  const unreadTotal = useMemo(() => rooms.rooms.reduce((sum, room) => sum + (room.muted ? 0 : room.unreadCount), 0), [rooms.rooms]);

  return <MessagingWorkspaceContext.Provider value={{
    activeRoomId: rooms.activeRoomId,
    directoryProps,
    roomProps,
    selectAgentDm: rooms.selectAgentDm,
    openMessage,
    openSession,
  }}><MessageNotifications activeRoomId={rooms.activeRoomId} threadId={threadId} roomVisible={subscribeToRoom}
    onOpen={(roomId, targetThreadId) => {
      rooms.selectRoom(roomId);
      setNotificationTarget({ roomId, threadId: targetThreadId });
      activateMessagesPanel();
    }} /><ShortcutSheet open={shortcutsOpen} onOpenChange={setShortcutsOpen} /><UnreadTitleBadge unread={unreadTotal} />{children}
    {selectRoomError && <div role="alert" className="px-4 py-2 text-sm text-err">{selectRoomError}{" "}
      <button type="button" className="underline underline-offset-4" onClick={() => setSelectErrorDismissed(rooms.selectRoomError)}>Dismiss</button>
    </div>}</MessagingWorkspaceContext.Provider>;
}
