"use client";

import { createContext, useContext, useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useAuth } from "@clerk/nextjs";
import { extractResultText, type CreateMessageRequest, type ListenMode, type MemberRef } from "@lightbulb/shared";
import { useRooms, type LiveRoomRun } from "@/hooks/useRooms";
import { useAgents } from "@/hooks/useAgents";
import { useRosterEntries } from "@/components/agents/useRosterEntries";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import type { CompanyAgent, CompanyMember } from "@/components/teams/shared/types";
import { activateMessagesPanel, openPanel } from "@/components/workspace/workspaceController";
import { MessageNotifications } from "@/components/notifications/MessageNotifications";
import type { RoomComposerProps } from "./RoomComposer";
import type { RoomDirectoryProps, ConversationCreation } from "./RoomDirectory";
import type { RoomPanelProps } from "./RoomPanel";
import type { DirectoryRoom, DirectoryParticipant, RoomParticipant, RoomRunView } from "./types";

function frameText(blocks: unknown): string | undefined {
  return extractResultText({ content: blocks }) || undefined;
}

function runView(run: LiveRoomRun): RoomRunView {
  const tools = new Map<string, RoomRunView["tools"][number]>();
  run.frames.forEach((frame, index) => {
    if (frame.type === "tool_start") {
      const id = frame.toolCallId ?? `${frame.runId}:${frame.tool}`;
      tools.set(id, { id, name: frame.tool, args: frame.args ?? {}, result: null, status: "running" });
    } else if (frame.type === "tool_progress") {
      const current = tools.get(frame.toolCallId) ?? {
        id: frame.toolCallId,
        name: frame.tool,
        args: {},
        result: null,
        status: "running" as const,
      };
      tools.set(frame.toolCallId, { ...current, progressText: frameText(frame.partialResult) });
    } else if (frame.type === "tool_end" || frame.type === "tool_error") {
      const id = frame.toolCallId ?? `${frame.runId}:${frame.tool}`;
      const current = tools.get(id);
      tools.set(id, {
        id,
        name: frame.tool,
        args: current?.args ?? {},
        result: frame.result ?? frame.meta ?? null,
        status: frame.type === "tool_error" ? "error" : "done",
        progressText: current?.progressText,
      });
    } else if (frame.type === "command_output") {
      const current = tools.get(frame.toolCallId);
      if (current && frame.output) {
        tools.set(frame.toolCallId, {
          ...current,
          progressText: `${current.progressText ?? ""}${frame.output}`,
        });
      }
    } else if (frame.type === "agent_activity" && frame.activity !== "run_status") {
      const id = `${frame.runId}:activity:${index}`;
      tools.set(id, {
        id,
        name: frame.title,
        args: frame.detail ?? {},
        result: null,
        status: "done",
        kind: "activity",
      });
    }
  });
  return { ...run, tools: [...tools.values()] };
}

export interface MessagingWorkspaceValue {
  activeRoomId: string | null;
  directoryProps: RoomDirectoryProps;
  roomProps: RoomPanelProps | null;
  selectAgentDm: (agentId: string) => Promise<string | null>;
}

const MessagingWorkspaceContext = createContext<MessagingWorkspaceValue | null>(null);

export function useMessagingWorkspace(): MessagingWorkspaceValue {
  const value = useContext(MessagingWorkspaceContext);
  if (!value) throw new Error("useMessagingWorkspace must be used within MessagingWorkspaceProvider");
  return value;
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
  const [mentions, setMentions] = useState<MemberRef[]>([]);
  const [clientMessageId, setClientMessageId] = useState(() => crypto.randomUUID());
  const [threadId, setThreadId] = useState<string | null>(null);
  const [notificationTarget, setNotificationTarget] = useState<{ roomId: string; threadId: string | null } | null>(null);
  useEffect(() => {
    if (!notificationTarget || notificationTarget.roomId !== rooms.activeRoomId) return;
    setThreadId(notificationTarget.threadId);
    setNotificationTarget(null);
  }, [notificationTarget, rooms.activeRoomId]);
  const [threadDraft, setThreadDraft] = useState("");
  const [threadMentions, setThreadMentions] = useState<MemberRef[]>([]);
  const [threadClientMessageId, setThreadClientMessageId] = useState(() => crypto.randomUUID());
  const clientMessageIdRef = useRef(clientMessageId);
  const threadClientMessageIdRef = useRef(threadClientMessageId);
  clientMessageIdRef.current = clientMessageId;
  threadClientMessageIdRef.current = threadClientMessageId;
  const [creationBusy, setCreationBusy] = useState(false);
  const [creationError, setCreationError] = useState<string>();
  const [draftRoomId, setDraftRoomId] = useState(rooms.activeRoomId);
  const typingActive = useRef(false);
  const typingTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Reset before the new room's composer consumes a pending draft/focus handoff.
  if (draftRoomId !== rooms.activeRoomId) {
    setDraftRoomId(rooms.activeRoomId);
    setDraft("");
    setMentions([]);
    setClientMessageId(crypto.randomUUID());
    setThreadId(null);
    setThreadDraft("");
    setThreadMentions([]);
    setThreadClientMessageId(crypto.randomUUID());
  }

  useEffect(() => () => {
    if (typingTimer.current) clearTimeout(typingTimer.current);
    if (typingActive.current) void setRoomTyping(false);
  }, [setRoomTyping]);

  const participants: RoomParticipant[] = useMemo(() => rooms.activeRoom?.members.map((member) => member.kind === "agent"
    ? { ...member, agentActivity: activityByAgentId.get(member.id) }
    : member) ?? [], [rooms.activeRoom, activityByAgentId]);
  const directoryRooms: DirectoryRoom[] = useMemo(() => rooms.rooms.map((room) => ({
    ...room,
    participants: (rooms.directoryRoomDetails.get(room.roomId)?.members ?? [])
      .filter((member) => member.kind !== "human" || member.id !== userId)
      .map((member) => member.kind === "agent"
        ? { ...member, agentActivity: activityByAgentId.get(member.id) }
        : member),
  })), [rooms.rooms, rooms.directoryRoomDetails, activityByAgentId, userId]);
  const directoryParticipants: DirectoryParticipant[] = useMemo(() => {
    const people = (humanRoster?.members ?? [])
      .filter((member) => member.userId !== userId)
      .map((member) => ({
      kind: "human" as const,
      id: member.userId,
      displayName: member.name || member.email || "Workspace member",
      mentionHandle: null,
    }));
    const agents = (agentRoster?.agents ?? []).map((agent) => ({
      kind: "agent" as const,
      id: agent.id,
      displayName: agent.name,
      mentionHandle: rooms.activeRoom?.members.find((member) => member.kind === "agent" && member.id === agent.id)?.mentionHandle ?? null,
    }));
    return [...people, ...agents];
  }, [humanRoster, agentRoster, rooms.activeRoom, userId]);

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

  const send = useCallback(async (request: CreateMessageRequest, thread = false) => {
    await sendRoomMessage(request);
    if (thread) {
      if (threadClientMessageIdRef.current !== request.clientMessageId) return;
      setThreadDraft("");
      setThreadMentions([]);
      setThreadClientMessageId(crypto.randomUUID());
    } else {
      if (clientMessageIdRef.current !== request.clientMessageId) return;
      setDraft("");
      setMentions([]);
      setClientMessageId(crypto.randomUUID());
    }
  }, [sendRoomMessage]);

  const agentCounterparts = participants.filter((member) => member.kind === "agent");
  const composer: RoomComposerProps = {
    focusAgentId: rooms.activeRoom?.kind === "dm" && agentCounterparts.length === 1 ? agentCounterparts[0].id : undefined,
    text: draft,
    onTextChange: setDraft,
    members: participants,
    selectedMentions: mentions,
    onMentionsChange: setMentions,
    clientMessageId,
    sendState: rooms.sendState(clientMessageId),
    onSubmit: (request) => send(request),
    onRetry: (request) => send(request),
    onReconcile: (id) => rooms.reconcileSend(id),
    disabled: !rooms.httpReady,
    onTypingChange: handleTyping,
    onUploadAttachment: rooms.uploadAttachment,
  };

  const threadComposer: { parentMessageId: string; composer: RoomComposerProps } | undefined = threadId ? {
    parentMessageId: threadId,
    composer: {
      text: threadDraft,
      onTextChange: setThreadDraft,
      members: participants,
      selectedMentions: threadMentions,
      onMentionsChange: setThreadMentions,
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

  const directoryState = rooms.roomsLoading || rooms.directoryLoading ? "loading" : rooms.roomsError || rooms.directoryError ? "error" : "ready";
  const roomState = rooms.roomLoading ? "loading" : rooms.accessRemoved ? "access-removed" : rooms.roomError ? "unavailable" : rooms.liveConnected ? "ready" : "reconnecting";
  const pending = rooms.optimisticMessages.filter((message) => message.clientMessageId === clientMessageId || message.clientMessageId === threadClientMessageId);

  const directoryProps: RoomDirectoryProps = {
    ownerScopeId: orgId ?? userId ?? "signed-out",
    rooms: directoryRooms,
    participants: directoryParticipants,
    state: directoryState,
    currentRoomId: rooms.activeRoomId ?? undefined,
    onSelectRoom: rooms.selectRoom,
    onSelectParticipant: selectParticipant,
    onCreate: createRoom,
    creationBusy,
    creationError,
    onRetry: () => void rooms.refreshRooms(),
  };
  const roomProps: RoomPanelProps | null = rooms.activeRoomId ? {
    ownerScopeId: orgId ?? userId ?? "signed-out",
    currentUserId: userId ?? "",
    roomId: rooms.activeRoomId,
    room: rooms.activeRoom,
    state: roomState,
    httpReady: rooms.httpReady,
    liveConnected: rooms.liveConnected,
    participants,
    messages: rooms.messages,
    historyLoading: rooms.historyLoading,
    historyComplete: rooms.historyComplete,
    historyError: rooms.historyError,
    onLoadHistory: rooms.loadHistory,
    pendingMessages: pending,
    typingMembers: rooms.typingMembers.filter((member) => member.userId !== userId),
    runs: rooms.runs.map(runView),
    composer,
    threadComposer,
    onSelectThread: setThreadId,
    selectedThreadId: threadId,
    onSelectParticipant: selectParticipant,
    onRetry: () => void rooms.refreshRoom(),
    onListenModeChange: (agentId, mode) => void changeListenMode(agentId, mode),
    onSetReaction: rooms.setReaction,
    ...(rooms.activeRoom?.kind === "channel" && ["owner", "admin"].includes(
      rooms.activeRoom.members.find((member) => member.kind === "human" && member.id === userId)?.role ?? "",
    ) ? {
      addMemberCandidates: directoryParticipants.filter((candidate) => !rooms.activeRoom!.members.some(
        (member) => member.kind === candidate.kind && member.id === candidate.id,
      )),
      onAddMember: addMember,
      onRenameRoom: async (name: string) => { await rooms.updateRoom({ name }); },
      onRemoveMember: rooms.removeMember,
    } : {}),
    onAbortRun: (runId) => void rooms.abortRun(runId),
    onSteerRun: (runId, text) => void rooms.steerRun(runId, text),
    onLinkToWork: () => openPanel("teams-issues", {}, { pushUrl: true }),
    onDownloadAttachment: rooms.downloadAttachment,
  } : null;

  return <MessagingWorkspaceContext.Provider value={{
    activeRoomId: rooms.activeRoomId,
    directoryProps,
    roomProps,
    selectAgentDm: rooms.selectAgentDm,
  }}><MessageNotifications activeRoomId={rooms.activeRoomId} threadId={threadId} roomVisible={subscribeToRoom}
    onOpen={(roomId, targetThreadId) => {
      rooms.selectRoom(roomId);
      setNotificationTarget({ roomId, threadId: targetThreadId });
      activateMessagesPanel();
    }} />{children}</MessagingWorkspaceContext.Provider>;
}
