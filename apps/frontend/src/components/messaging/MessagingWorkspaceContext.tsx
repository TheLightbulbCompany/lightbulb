"use client";

import { createContext, useContext, useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useAuth } from "@clerk/nextjs";
import { canChangeListenMode, canControlSession, canSendToSession, markSessionsSeen, resolveTypedMentions, runView, sessionArchived, sessionMentionCandidates, sessionUnread, useWorkspaceSessions, workspaceSections, type CreateMessageRequest, type ListenMode, type MemberRef, type RoomMember, type SessionSource, type RoomSummary } from "@lightbulb/shared";
import { useRooms } from "@/hooks/useRooms";
import { useBilling } from "@/hooks/useBilling";
import { useAgents } from "@/hooks/useAgents";
import { useRosterEntries } from "@/components/agents/useRosterEntries";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import type { CompanyAgent, CompanyMember } from "@/components/teams/shared/types";
import { activateMessagesPanel, openPanel } from "@/components/workspace/workspaceController";
import { MessageNotifications } from "@/components/notifications/MessageNotifications";
import { UnreadTitleBadge } from "@/components/notifications/UnreadTitleBadge";
import { hasBlockingShortcutDialog } from "@/components/teams/shared/lib/keyboardShortcuts";
import { capture } from "@/lib/analytics";
import { clearDraft, draftKey, listDraftRoomIds, readDraft, writeDraft } from "./drafts";
import { adjacentRoomId, nextUnreadRoomId } from "./roomNavigation";
import { usePresence } from "./usePresence";
import { readSessionSeen, writeSessionSeen } from "./sidebarPrefs";
import { ShortcutSheet } from "./ShortcutSheet";
import type { RoomComposerProps } from "./RoomComposer";
import type { RoomDirectoryProps, ConversationCreation } from "./RoomDirectory";
import type { RoomPanelProps } from "./RoomPanel";
import type { SessionFocusTarget } from "./SessionFocus";
import type { SidebarSession } from "./roomSections";
import type { DirectoryRoom, DirectoryParticipant, RoomParticipant } from "./types";

export interface MessagingWorkspaceValue {
  activeRoomId: string | null;
  directoryProps: RoomDirectoryProps;
  roomProps: RoomPanelProps | null;
  /** `explicit` false only for the default-agent fallback, which isn't a user's pick. */
  selectAgentDm: (agentId: string, explicit?: boolean) => Promise<string | null>;
  openMessage: (roomId: string, messageId: string | null) => void;
  openSession: (sessionId: string) => void;
  /** Focus an empty new session (the composer that starts one on your Mac). */
  openNewSession: () => void;
  sessionFocus: SessionFocusTarget | null;
  closeSession: () => void;
  /** Not gated on the active dock panel: a split dock keeps the focused session on screen. */
  sessionSource: SessionSource;
  /** ownerName/own for a session fetched directly, outside the workspace session list. */
  resolveSessionOwner: (humanUserId: string) => { ownerName: string; own: boolean };
  workspaceSessions: ReturnType<typeof useWorkspaceSessions>;
  scope: string;
  /** Latched at the room list's first load per workspace; null until then. */
  freshLanding: boolean | null;
}

/** Nothing but #general and the vendor shared room: a new workspace, whose home is #general. */
export function isFreshWorkspace(rooms: readonly RoomSummary[]): boolean {
  return rooms.every((room) => room.isDefault || room.shared);
}

/**
 * Where a default-agent request lands, only once it is certain: any other room
 * means returning (false); #general with nothing but the shared room beside it
 * means fresh (true); [] or shared-only means #general is not seeded yet (null).
 */
function landingFor(rooms: readonly RoomSummary[]): boolean | null {
  if (!isFreshWorkspace(rooms)) return false;
  return rooms.some((room) => room.isDefault) ? true : null;
}

// ponytail: fixed wait — provisioning seeds #general in ~15–50 s; a workspace
// that never gets one (legacy, #general left) opens the default DM a minute late.
// Make it event-driven if the backend ever says "seeded" explicitly.
const FRESH_LANDING_WAIT_MS = 60_000;

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
  defaultAgentId,
  subscribeToRoom = true,
  observeRuns = false,
}: {
  children: ReactNode;
  initialAgentId?: string | null;
  /** Opened only when nothing explicit was asked for and the workspace is not fresh. */
  defaultAgentId?: string | null;
  subscribeToRoom?: boolean;
  observeRuns?: boolean;
}) {
  const { userId, orgId } = useAuth();
  const scope = orgId ?? userId ?? "signed-out";
  // Focus placement: a session opened from the sidebar or a link takes the
  // middle of the panel; sessions live in the workspace, not in a room.
  const [sessionFocus, setSessionFocus] = useState<SessionFocusTarget | null>(null);
  // Focus covers the room. A covered room must neither auto-read nor silence its notifications.
  const roomVisible = subscribeToRoom && !sessionFocus;
  // Browser watches run frames even when the room is hidden; visibility still
  // controls read receipts and notification suppression.
  // Fresh or returning is decided once per workspace, as soon as the room list
  // makes it certain: a DM created later must not turn a new workspace
  // "returning" and pull the viewer into the default agent's DM mid-onboarding.
  const [landing, setLanding] = useState<{ scope: string; fresh: boolean } | null>(null);
  const freshLanding = landing?.scope === scope ? landing.fresh : null;
  const rooms = useRooms(
    defaultAgentId && freshLanding === false ? initialAgentId ?? defaultAgentId : initialAgentId,
    subscribeToRoom || observeRuns,
    roomVisible,
  );
  const observedLanding = rooms.roomsLoading || rooms.roomsError ? null : landingFor(rooms.rooms);
  if (freshLanding === null && observedLanding !== null) setLanding({ scope, fresh: observedLanding });
  const awaitingGeneral = freshLanding === null && !rooms.roomsLoading;
  useEffect(() => {
    if (!awaitingGeneral) return;
    const timer = setTimeout(() => setLanding((current) => current?.scope === scope ? current : { scope, fresh: false }), FRESH_LANDING_WAIT_MS);
    return () => clearTimeout(timer);
  }, [awaitingGeneral, scope]);
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
  // Read in the send failure path, which resolves long after its closure was
  // built and must know where the owner actually is now.
  const activeRoomIdRef = useRef(rooms.activeRoomId);
  activeRoomIdRef.current = rooms.activeRoomId;
  const threadIdRef = useRef<string | null>(threadId);
  threadIdRef.current = threadId;
  const activeRoomKind = rooms.activeRoom?.kind;
  const activeRoomKindRef = useRef(activeRoomKind);
  activeRoomKindRef.current = activeRoomKind;
  // One room_opened per room the person lands in: explicit picks and the
  // fresh-workspace auto-open of #general alike. room_kind only — a room's
  // name can be customer content. Before this, the only trace that a new
  // owner had seen the welcome message was a read cursor in DynamoDB.
  useEffect(() => {
    if (!rooms.activeRoomId || !activeRoomKind) return;
    capture("room_opened", { room_kind: activeRoomKind, workspace_id: orgId ?? null });
  }, [rooms.activeRoomId, activeRoomKind, orgId]);
  // composer_typing_started, once per room per mount: separates "looked and
  // left" from "started to type and gave up" for a new owner who never sends.
  const typedRoomsRef = useRef<Set<string>>(new Set());
  // Mirror the live composer text. Read in the send failure path to decide
  // whether there is anything there worth protecting.
  const draftRef = useRef(draft);
  draftRef.current = draft;
  const threadDraftRef = useRef(threadDraft);
  threadDraftRef.current = threadDraft;
  const [creationBusy, setCreationBusy] = useState(false);
  const [openingParticipant, setOpeningParticipant] = useState<string | null>(null);
  // Bumped whenever a draft is written for a room OTHER than the active one —
  // which only the send-failure path does. Without it the sidebar's draft dot
  // and Drafts section would not show a recovered draft until some unrelated
  // render happened to recompute them.
  const [recoveredDrafts, setRecoveredDrafts] = useState(0);
  const [creationError, setCreationError] = useState<string>();
  // A failed DM resolve-or-create (rooms.selectRoomError) otherwise vanishes:
  // the caller rethrows but nobody renders it. Dismissal is local — the hook
  // itself clears the error on the next successful select or create.
  const [selectErrorDismissed, setSelectErrorDismissed] = useState<string | null>(null);
  const selectRoomError = rooms.selectRoomError && rooms.selectRoomError !== selectErrorDismissed
    ? rooms.selectRoomError
    : null;
  // A fresh workspace has no rooms until setup seeds #general, so the viewer
  // sits on an empty pane (or on the vendor shared room, which the shared
  // hook's rooms[0] pick can land on). Open #general once per workspace — but
  // only while the workspace is FRESH (nothing but #general and the shared
  // room exists) and the viewer has not chosen a room themselves. A returning
  // viewer has other rooms, so the shared hook's most-recent pick stands.
  // Both refs hold the scope they apply to, so switching workspaces re-arms.
  const explicitChoiceScope = useRef<string | null>(null);
  const autoOpenedScope = useRef<string | null>(null);
  const markExplicitChoice = useCallback(() => { explicitChoiceScope.current = scope; }, [scope]);
  const { activeRoomId: autoOpenActive, rooms: autoOpenRooms, selectRoom: hookSelectRoom } = rooms;
  /** Every selection a person (or a link/notification they followed) makes. */
  const chooseRoom = useCallback((roomId: string) => {
    markExplicitChoice();
    hookSelectRoom(roomId);
  }, [markExplicitChoice, hookSelectRoom]);
  useEffect(() => {
    if (autoOpenedScope.current === scope || explicitChoiceScope.current === scope) return;
    const general = autoOpenRooms.find((room) => room.isDefault);
    if (!general || !isFreshWorkspace(autoOpenRooms)) return;
    autoOpenedScope.current = scope;
    if (autoOpenActive !== general.roomId) hookSelectRoom(general.roomId);
  }, [scope, autoOpenActive, autoOpenRooms, hookSelectRoom]);
  const [shortcutsOpen, setShortcutsOpen] = useState(false);
  const toggleShortcuts = useCallback(() => setShortcutsOpen((open) => !open), []);
  useMessagingShortcuts(rooms.rooms, rooms.activeRoomId, chooseRoom, toggleShortcuts, shortcutsOpen);
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
  // Ended sessions come too: All sessions lists the last week's.
  // The session sidebar stays visible when Tasks or another dock panel has focus.
  const workspaceSessions = useWorkspaceSessions(rooms.resourceSource, { enabled: rooms.httpReady, includeEnded: true });
  const sharing = useMemo(() => new Set(workspaceSessions.sessions.filter((session) => session.status !== "ended" && !sessionArchived(session))
    .map((session) => session.humanUserId)), [workspaceSessions.sessions]);
  // The unread dot: this viewer's last-seen seq per session, on this device.
  // ponytail: entries are never pruned (one number per session ever listed); prune if it ever matters.
  const [sessionSeen, setSessionSeen] = useState(() => ({ scope, seen: readSessionSeen(scope) }));
  const seen = sessionSeen.scope === scope ? sessionSeen.seen : readSessionSeen(scope);
  const openSessionId = sessionFocus?.sessionId ?? null;
  useEffect(() => {
    const next = markSessionsSeen(seen, workspaceSessions.sessions, openSessionId);
    if (next === seen && sessionSeen.scope === scope) return;
    setSessionSeen({ scope, seen: next });
    writeSessionSeen(scope, next);
  }, [seen, workspaceSessions.sessions, openSessionId, scope, sessionSeen.scope]);
  // A room member's online/sharing (human) or agentActivity (agent) decoration --
  // the same ternary every member list below applies to its own room-member array.
  const decorate = useCallback((member: RoomMember): RoomParticipant => member.kind === "agent"
    ? { ...member, agentActivity: activityByAgentId.get(member.id) }
    : { ...member, online: online.has(member.id), sharing: sharing.has(member.id) },
  [activityByAgentId, online, sharing]);
  // A roster row's display name, with the same "Workspace member" fallback everywhere one is missing.
  const rosterName = (member: CompanyMember | undefined) => member?.name || member?.email || "Workspace member";
  const humanName = useCallback((id: string) => rosterName(humanRoster?.members?.find((candidate) => candidate.userId === id)), [humanRoster]);
  // The same owner-name/own resolution sidebarSessions applies per section, for a
  // session fetched directly rather than found in the workspace list (SessionFocus's
  // single-session fallback).
  const resolveSessionOwner = useCallback((humanUserId: string) =>
    ({ ownerName: humanUserId === userId ? "You" : humanName(humanUserId), own: humanUserId === userId }),
    [userId, humanName]);
  const sidebarSessions: SidebarSession[] = useMemo(() =>
    workspaceSections(workspaceSessions.sessions, userId, humanName).flatMap((section) =>
      section.data.map((session) => ({ session, ownerName: section.title, own: section.key === userId, unread: sessionUnread(session, seen) }))),
    [workspaceSessions.sessions, userId, humanName, seen]);
  const participants: RoomParticipant[] = useMemo(() => rooms.activeRoom?.members.map(decorate) ?? [], [rooms.activeRoom, decorate]);
  const sessionCandidates = useMemo(() => sessionMentionCandidates(
    workspaceSessions.sessions.filter((session) => session.kind !== "background"),
  ), [workspaceSessions.sessions]);
  // What a reference card resolves against: every interactive session the viewer can open,
  // not the sendable subset below (an offline, ended or read-only teammate session is still a reference).
  const visibleSessions = useMemo(() => sessionCandidates.map(({ session }) => session), [sessionCandidates]);
  const sessionTargets = useMemo(() => rooms.activeRoom?.shared ? [] : sessionCandidates
    .filter(({ session }) => session.shared !== false && canSendToSession(session)
      && rooms.activeRoom?.members.some((member) => member.kind === "human" && member.id === session.humanUserId)
      && canControlSession(session, session.humanUserId === userId, userId))
    .map((candidate) => ({ candidate, ...resolveSessionOwner(candidate.session.humanUserId) })),
  [rooms.activeRoom?.shared, rooms.activeRoom?.members, sessionCandidates, userId, resolveSessionOwner]);
  const directoryRooms: DirectoryRoom[] = useMemo(() => rooms.rooms.map((room) => ({
    ...room,
    // Straight off the room summary — this used to come from a per-DM
    // `getRoom` fan-out, so until it settled every DM row read "Direct message".
    participants: (room.members ?? [])
      .filter((member) => member.kind !== "human" || member.id !== userId)
      .map(decorate),
  })), [rooms.rooms, userId, decorate]);
  const directoryParticipants: DirectoryParticipant[] = useMemo(() => {
    const people = (humanRoster?.members ?? [])
      .filter((member) => member.userId !== userId)
      .map((member) => ({
      kind: "human" as const,
      id: member.userId,
      displayName: rosterName(member),
      mentionHandle: null,
      online: online.has(member.userId),
      sharing: sharing.has(member.userId),
    }));
    const agents = (agentRoster?.agents ?? []).map((agent) => ({
      kind: "agent" as const,
      id: agent.id,
      displayName: agent.name,
      agentActivity: activityByAgentId.get(agent.id),
      mentionHandle: rooms.activeRoom?.members.find((member) => member.kind === "agent" && member.id === agent.id)?.mentionHandle ?? null,
    }));
    return [...people, ...agents];
  }, [humanRoster, agentRoster, rooms.activeRoom, userId, online, sharing, activityByAgentId]);

  const handleTyping = useCallback((isTyping: boolean) => {
    if (typingTimer.current) clearTimeout(typingTimer.current);
    if (!isTyping) {
      if (typingActive.current) void setRoomTyping(false);
      typingActive.current = false;
      return;
    }
    if (!typingActive.current) {
      void setRoomTyping(true);
      const roomId = activeRoomIdRef.current;
      if (roomId && !typedRoomsRef.current.has(roomId)) {
        typedRoomsRef.current.add(roomId);
        capture("composer_typing_started", {
          room_kind: activeRoomKindRef.current ?? null,
          workspace_id: orgId ?? null,
        });
      }
    }
    typingActive.current = true;
    typingTimer.current = setTimeout(() => {
      typingActive.current = false;
      void setRoomTyping(false);
    }, 3_000);
  }, [setRoomTyping, orgId]);

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

  /**
   * Park a failed message's text in a room's stored draft — but never on top of
   * something newer.
   *
   * There is one draft slot per room and, in this path, two possible claimants:
   * the message that just failed and whatever the owner has typed since. The
   * live text is the owner's current intent and exists nowhere else, so it
   * wins; the failed text is dropped rather than allowed to overwrite it.
   *
   * ponytail: a failed message should live in the timeline as a failed row with
   * its own retry, the way Slack does it, which removes the contention
   * entirely. That needs a rejected state on the pending row in RoomPanel —
   * `useRooms` currently drops the optimistic entry on failure — and is not
   * this change.
   */
  const recoverDraft = useCallback((key: string, request: CreateMessageRequest) => {
    if (readDraft(key).text.trim()) return;
    writeDraft(key, { text: request.text, mentions: request.mentions });
    setRecoveredDrafts((count) => count + 1);
  }, []);

  const send = useCallback(async (request: CreateMessageRequest, thread = false) => {
    // Read before the await: this is the room the message was sent FROM, even
    // if the owner switches rooms while it is in flight.
    const roomId = rooms.activeRoomId;
    // Empty the box NOW, before the round trip — not after it.
    //
    // This used to await the POST first, and because `sendState` is "sending"
    // for that whole flight the textarea was also disabled, so a sender watched
    // their own text sit frozen in the composer while an optimistic copy of it
    // rendered in the timeline below. Clearing on the keystroke is what makes
    // sending feel instant; the optimistic row already carries the pending,
    // failed and uncertain states, and `reconcileSend` still holds the request
    // by its client id, so nothing is lost if the send does not land.
    let rotatedTo: string | null = null;
    if (thread) {
      if (roomId && request.threadRootMessageId) clearDraft(draftKey(scope, roomId, request.threadRootMessageId));
      if (threadClientMessageIdRef.current === request.clientMessageId) {
        rotatedTo = crypto.randomUUID();
        // The ref mirrors the composer's CURRENT id, so it has to move now
        // rather than at the next render — a send that rejects in the same
        // microtask would otherwise read a stale ref and conclude the composer
        // had moved on when it had not.
        threadClientMessageIdRef.current = rotatedTo;
        threadDraftRef.current = "";
        setThreadDraft("");
        setThreadClientMessageId(rotatedTo);
      }
    } else {
      if (roomId) clearDraft(draftKey(scope, roomId, null));
      if (clientMessageIdRef.current === request.clientMessageId) {
        rotatedTo = crypto.randomUUID();
        clientMessageIdRef.current = rotatedTo;   // see the thread branch above
        draftRef.current = "";
        setDraft("");
        setClientMessageId(rotatedTo);
      }
    }
    try {
      await sendRoomMessage(request);
    } catch (error) {
      // Give it back. The composer has already moved on to a fresh id, so
      // without this the failed message has nowhere to live: its optimistic row
      // is dropped by `useRooms` on failure, its text was cleared, and both
      // "Retry message" and "Check message status" would act on an id that
      // tracks nothing. Re-adopting the failed id is what makes `sendState`
      // report rejected/uncertain again and points retry at the real request.
      const sameRoom = activeRoomIdRef.current === roomId;
      // ...but only into the composer this send actually left behind. Sending a
      // second message, or typing into the fresh box, rotates the id again —
      // and re-adopting the failed one then remounts the composer and destroys
      // that newer, unsent work (its text and any attached-but-unsent images).
      // Newer work wins: it is the owner's live intent, and unlike the failed
      // message it exists nowhere else.
      // Two ways the composer stops being the empty box this send left behind,
      // and BOTH have to be checked: sending again rotates the id, but simply
      // TYPING does not — it only fills the box. Restoring over either is the
      // same data loss, so the box must still be the one we rotated to AND
      // still be empty.
      const composerHeld = !thread && rotatedTo !== null
        && clientMessageIdRef.current === rotatedTo && draftRef.current.trim() === "";
      const threadComposerHeld = thread && rotatedTo !== null
        && threadClientMessageIdRef.current === rotatedTo && threadDraftRef.current.trim() === "";
      if (thread) {
        if (sameRoom && threadComposerHeld && threadIdRef.current === request.threadRootMessageId) {
          setThreadDraft(request.text);
          setThreadClientMessageId(request.clientMessageId);
        } else if (roomId && request.threadRootMessageId) {
          recoverDraft(draftKey(scope, roomId, request.threadRootMessageId), request);
        }
      } else if (sameRoom && composerHeld) {
        setDraft(request.text);
        setClientMessageId(request.clientMessageId);
      } else if (roomId) {
        // Nowhere live to put it — the owner is in another room, or has moved
        // the composer on. It goes to that room's stored draft instead.
        recoverDraft(draftKey(scope, roomId, null), request);
      }
      // Rethrown: the composer's own submit() turns a rejection into its
      // uncertain state, and that path must keep working.
      throw error;
    }
  }, [sendRoomMessage, rooms.activeRoomId, scope, recoverDraft]);

  // localStorage is the source, so no prop dependency truly describes when this
  // changes — but it walks every key in the store and JSON.parses each hit, and
  // every writer is in this component: the draft effects below, and the
  // send-failure path, which is the only one that can write for a room the
  // owner has already left (hence `recoveredDrafts`). Recomputing when one of
  // those moves is enough, and keeps the walk off the keystroke path for every
  // other room's row.
  const draftRoomIds = useMemo(
    () => listDraftRoomIds(scope),
    [scope, draft, threadDraft, rooms.activeRoomId, recoveredDrafts],
  );

  const agentCounterparts = participants.filter((member) => member.kind === "agent");
  const runningAgentIds = rooms.runs.filter((run) => run.state === "running").map((run) => run.agentId);
  const { account: billingAccount, refresh: refreshBilling } = useBilling();
  // A system notice is the platform reporting a budget block on a run; re-read
  // the account so the limit bar appears with it (the read never polls).
  const lastSystemNoticeId = rooms.messages.findLast((message) => message.author.kind === "system")?.messageId;
  useEffect(() => {
    if (lastSystemNoticeId) void refreshBilling();
  }, [lastSystemNoticeId, refreshBilling]);
  const composer: RoomComposerProps = {
    focusAgentId: rooms.activeRoom?.kind === "dm" && agentCounterparts.length === 1 ? agentCounterparts[0].id : undefined,
    text: draft,
    onTextChange: setDraft,
    members: participants,
    sessionTargets,
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
      sessionTargets,
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
      markExplicitChoice();
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
    // Only the row being opened goes busy. This used to set `creationBusy`,
    // which disables EVERY roster row, so opening one conversation froze the
    // whole list for the duration of the round trip.
    setOpeningParticipant(`${participant.kind}:${participant.id}`);
    setCreationError(undefined);
    try {
      markExplicitChoice();
      await rooms.selectDm(participant);
    } catch (error) {
      setCreationError(error instanceof Error ? error.message : "Conversation could not be opened.");
    } finally {
      setOpeningParticipant(null);
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
      listenMode: member.kind === "agent" ? "active" : null,
    });
  }

  // Permalink target: opened via openMessage (a room permalink), cleared 3s
  // after opening rather than persisting indefinitely once the highlight has
  // had time to land.
  const [target, setTarget] = useState<{ roomId: string; messageId: string | null } | null>(null);
  const selectRoom = chooseRoom;
  const selectDm = rooms.selectDm;
  // A permalink applied before the workspace session list lands (a cold load,
  // a new tab, a deep link) cannot yet tell a live session from an ended one,
  // so it waits here and is replayed once the list settles.
  const heldLink = useRef<string | null>(null);
  const closeSession = useCallback(() => { heldLink.current = null; setSessionFocus(null); }, []);
  const openSession = useCallback((sessionId: string) => {
    heldLink.current = null; // the latest intent wins
    if (workspaceSessions.loading && !workspaceSessions.sessions.some((candidate) => candidate.sessionId === sessionId)) {
      heldLink.current = sessionId;
      return;
    }
    setThreadId(null);
    setTarget(null);
    setSessionFocus({ sessionId });
  }, [workspaceSessions.sessions, workspaceSessions.loading]);
  const openNewSession = useCallback(() => {
    heldLink.current = null;
    setThreadId(null);
    setTarget(null);
    setSessionFocus({ sessionId: null });
  }, []);
  const openAllSessions = useCallback(() => {
    heldLink.current = null;
    setThreadId(null);
    setTarget(null);
    setSessionFocus({ sessionId: null, all: true });
  }, []);
  useEffect(() => {
    const link = heldLink.current;
    if (!link || workspaceSessions.loading) return;
    openSession(link);
  }, [workspaceSessions.loading, openSession]);
  const openMessage = useCallback((roomId: string, messageId: string | null) => {
    closeSession();
    selectRoom(roomId);
    setTarget({ roomId, messageId });
  }, [selectRoom, closeSession]);
  const previousSelectedRoom = useRef(rooms.activeRoomId);
  useEffect(() => {
    const moved = previousSelectedRoom.current !== rooms.activeRoomId;
    // Room changes that bypass the directory (a notification, ⌥↑/⌥↓) leave focus
    // too. A cold load's automatic first room is not a move away from anything.
    if (moved && previousSelectedRoom.current !== null) setSessionFocus(null);
    previousSelectedRoom.current = rooms.activeRoomId;
  }, [rooms.activeRoomId]);
  const targetRoomId = rooms.activeRoomId;
  const targetMessages = rooms.messages;
  const targetHistoryComplete = rooms.historyComplete;
  const targetHistoryLoading = rooms.historyLoading;
  const targetHistoryError = rooms.historyError;
  const loadTargetHistory = rooms.loadHistory;
  useEffect(() => {
    if (!target?.messageId) return;
    if (target.roomId !== targetRoomId) return;
    if (!targetMessages.some((message) => message.messageId === target.messageId) && !targetHistoryComplete && !targetHistoryLoading && !targetHistoryError) {
      loadTargetHistory();
      return;
    }
    if (!targetHistoryComplete && !targetMessages.some((message) => message.messageId === target.messageId) && !targetHistoryError) return;
    const timer = setTimeout(() => setTarget(null), 3_000);
    return () => clearTimeout(timer);
  }, [target, targetRoomId, targetMessages, targetHistoryComplete, targetHistoryLoading, targetHistoryError, loadTargetHistory]);

  const readSeqAtOpen = useRef<{ roomId: string; seq: number | null } | null>(null);
  if (rooms.activeRoom && readSeqAtOpen.current?.roomId !== rooms.activeRoom.roomId) readSeqAtOpen.current = { roomId: rooms.activeRoom.roomId, seq: rooms.activeRoom.lastReadSeq ?? null };

  // `directoryLoading` is gone with the fan-out that produced it: the room list
  // is now the whole sidebar, so there is nothing else to wait for.
  const directoryState = rooms.roomsLoading ? "loading" : rooms.roomsError ? "error" : "ready";
  // Messages in hand beat a pending room-detail read. The detail GET only adds
  // the delivery key and pinned ids; blocking the whole panel on it meant a
  // room you opened a moment ago still showed "Loading conversation…".
  const roomState = rooms.roomLoading && rooms.messages.length === 0 ? "loading"
    : rooms.accessRemoved ? "access-removed"
    : rooms.roomError ? "unavailable"
    : rooms.liveConnected ? "ready" : "reconnecting";
  // Every message still in flight for this room, NOT just the one the composer
  // happens to be pointing at. The composer rotates to a fresh id the moment a
  // send leaves, so filtering by it here meant the "Sending…" bubble matched
  // nothing and the message vanished until the server answered — the opposite
  // of the point. `useRooms` already scopes these to the active room and clears
  // them on a room change, and RoomPanel splits main-timeline from thread.
  const pending = rooms.optimisticMessages;
  const viewerRole = rooms.activeRoom?.members.find((member) => member.kind === "human" && member.id === userId)?.role ?? "";
  // Any member may change an agent's response, on every plan (owner ruling 2026-09-24).
  const canManageResponses = canChangeListenMode(viewerRole);

  const directoryProps: RoomDirectoryProps = {
    ownerScopeId: scope,
    rooms: directoryRooms,
    sections: rooms.sections,
    participants: directoryParticipants,
    state: directoryState,
    currentRoomId: rooms.activeRoomId ?? undefined,
    // Picking any conversation leaves focus placement — the session goes back
    // to being something you open, not the thing you are looking at.
    onSelectRoom: (roomId: string) => { closeSession(); chooseRoom(roomId); },
    onSelectParticipant: async (participant: DirectoryParticipant) => { closeSession(); await selectParticipant(participant); },
    openingParticipant,
    onMarkUnread: rooms.markUnread,
    onSetPrefs: rooms.setRoomPrefs,
    onSaveSections: rooms.saveSections,
    onLeaveRoom: rooms.leaveRoom,
    onCreate: createRoom,
    creationBusy,
    creationError,
    onRetry: () => void rooms.refreshRooms(),
    draftRoomIds,
    sessions: sidebarSessions,
    onSelectSession: (sessionId: string) => void openSession(sessionId),
    onRenameSession: rooms.resourceSource.client.setLocalSessionDisplayTitle ? async (sessionId, title) => {
      await rooms.resourceSource.client.setLocalSessionDisplayTitle!(sessionId, title);
      await workspaceSessions.refresh();
    } : undefined,
    currentSessionId: sessionFocus?.sessionId ?? null,
    onOpenAllSessions: openAllSessions,
    allSessionsOpen: !!sessionFocus?.all,
  };
  const selectMessageParticipant = useCallback(async (participant: RoomParticipant) => {
    markExplicitChoice();
    await selectDm(participant);
  }, [markExplicitChoice, selectDm]);
  const { selectAgentDm: hookSelectAgentDm } = rooms;
  const selectAgentDm = useCallback((agentId: string, explicit = true) => {
    if (explicit) markExplicitChoice();
    return hookSelectAgentDm(agentId);
  }, [markExplicitChoice, hookSelectAgentDm]);
  const { activeRoomId: unreadRoomId, markUnread: markRoomUnread } = rooms;
  const markUnreadFrom = useCallback(async (fromSeq: number) => {
    if (unreadRoomId) await markRoomUnread(unreadRoomId, fromSeq);
  }, [unreadRoomId, markRoomUnread]);
  const roomProps: RoomPanelProps | null = rooms.activeRoomId ? {
    ownerScopeId: scope,
    currentUserId: userId ?? "",
    roomId: rooms.activeRoomId,
    room: rooms.activeRoom,
    readSeqAtOpen: readSeqAtOpen.current?.roomId === rooms.activeRoomId ? readSeqAtOpen.current.seq : null,
    targetMessageId: target?.roomId === rooms.activeRoomId ? target.messageId : null,
    state: roomState,
    httpReady: rooms.httpReady,
    liveConnected: rooms.liveConnected,
    participants,
    messages: rooms.messages,
    historyLoading: rooms.historyLoading,
    historyComplete: rooms.historyComplete,
    historyError: rooms.historyError,
    historyErrorMessage: rooms.historyErrorMessage,
    onLoadHistory: rooms.loadHistory,
    onLoadOlder: rooms.loadOlder,
    pendingMessages: pending,
    typingMembers: rooms.typingMembers.filter((member) => member.userId !== userId),
    runs: rooms.runs.map(runView),
    composer,
    threadComposer,
    sessions: visibleSessions,
    onSelectThread: setThreadId,
    selectedThreadId: threadId,
    onSelectParticipant: selectMessageParticipant,
    onRetry: () => void rooms.refreshRoom(),
    ...(canManageResponses ? { onListenModeChange: (agentId: string, mode: ListenMode) => void changeListenMode(agentId, mode) } : {}),
    billing: billingAccount,
    onSetReaction: rooms.setReaction,
    onUpdateMessage: rooms.updateMessage,
    pinnedMessageIds: rooms.activeRoom?.pinnedMessageIds,
    onSetPin: rooms.setPin,
    onMarkUnreadFrom: markUnreadFrom,
    onSetPrefs: async (prefs) => { if (rooms.activeRoomId) await rooms.setRoomPrefs(rooms.activeRoomId, prefs); },
    // A group chat's creator holds no powers there, so anyone may leave or archive one.
    onLeaveRoom: rooms.activeRoom?.kind === "group-dm" || (rooms.activeRoom?.kind === "channel" && viewerRole !== "owner")
      ? async () => { if (rooms.activeRoomId) await rooms.leaveRoom(rooms.activeRoomId); } : undefined,
    ...(rooms.activeRoom?.kind === "group-dm" || (rooms.activeRoom?.kind === "channel" && ["owner", "admin"].includes(viewerRole))
      ? { onSetArchived: async (archived: boolean) => { await rooms.updateRoom({ archived }); } } : {}),
    // Only the channel's owner -- not an admin -- may delete it (the backend enforces the same).
    ...(rooms.activeRoom?.kind === "channel" && viewerRole === "owner"
      ? { onDeleteRoom: async () => { if (rooms.activeRoomId) await rooms.deleteRoom(rooms.activeRoomId); } } : {}),
    ...(rooms.activeRoom && rooms.activeRoom.kind !== "dm" && ["owner", "admin"].includes(viewerRole) ? {
      onRenameRoom: async (name: string) => { await rooms.updateRoom({ name }); },
      onSetTopic: async (topic: string | null) => { await rooms.updateRoom({ topic }); },
    } : {}),
    ...(rooms.activeRoom && !rooms.activeRoom.shared
      && (rooms.activeRoom.kind !== "channel" || ["owner", "admin"].includes(viewerRole)) ? {
      addMemberCandidates: directoryParticipants.filter((candidate) => !rooms.activeRoom!.members.some(
        (member) => member.kind === candidate.kind && member.id === candidate.id,
      )),
      onAddMember: addMember,
    } : {}),
    ...(rooms.activeRoom?.kind === "channel" && ["owner", "admin"].includes(viewerRole)
      ? { onRemoveMember: rooms.removeMember } : {}),
    onAbortRun: (runId) => rooms.abortRun(runId),
    onLinkToWork: () => openPanel("teams-issues", {}, { pushUrl: true }),
    onDownloadAttachment: rooms.downloadAttachment,
    onAttachmentUrl: rooms.attachmentUrl,
  } : null;

  // Muted rooms never badge anywhere: not the sidebar, not the browser-tab title.
  const openNotificationTarget = useCallback((roomId: string, targetThreadId: string | null, messageId?: string) => {
    // The room may be the one focus covers; opening it has to uncover it.
    closeSession();
    selectRoom(roomId);
    setNotificationTarget({ roomId, threadId: targetThreadId });
    // A room-level message is scrolled to, like a search hit; a thread reply is in the opened thread.
    if (messageId && !targetThreadId) setTarget({ roomId, messageId });
    activateMessagesPanel();
  }, [closeSession, selectRoom]);
  // A failed-run toast (RunNotifications) opens the room the run ran in.
  useEffect(() => {
    const onOpenRoom = (event: Event) => {
      const detail = (event as CustomEvent<{ roomId?: string; threadId?: string | null; sessionId?: string }>).detail;
      // A session notification or Activity item carries a session instead of a room.
      if (detail?.sessionId) { openSession(detail.sessionId); activateMessagesPanel(); return; }
      if (detail?.roomId) openNotificationTarget(detail.roomId, detail.threadId ?? null);
    };
    window.addEventListener("openRoom", onOpenRoom);
    return () => window.removeEventListener("openRoom", onOpenRoom);
  }, [openNotificationTarget, openSession]);

  const unreadTotal = useMemo(() => rooms.rooms.reduce((sum, room) => sum + (room.muted ? 0 : room.unreadCount), 0), [rooms.rooms]);

  return <MessagingWorkspaceContext.Provider value={{
    activeRoomId: rooms.activeRoomId,
    directoryProps,
    roomProps,
    selectAgentDm,
    openMessage,
    openSession,
    openNewSession,
    sessionFocus,
    closeSession,
    sessionSource: rooms.resourceSource,
    resolveSessionOwner,
    workspaceSessions,
    scope,
    freshLanding,
  }}><MessageNotifications activeRoomId={rooms.activeRoomId} threadId={threadId} roomVisible={roomVisible} rooms={rooms.rooms}
    onOpen={openNotificationTarget} activeSessionId={sessionFocus?.sessionId ?? null}
    onOpenSession={(sessionId) => { openSession(sessionId); activateMessagesPanel(); }} /><ShortcutSheet open={shortcutsOpen} onOpenChange={setShortcutsOpen} /><UnreadTitleBadge unread={unreadTotal} />{children}
    {selectRoomError && <div role="alert" className="px-4 py-2 text-sm text-err">{selectRoomError}{" "}
      <button type="button" className="underline underline-offset-4" onClick={() => setSelectErrorDismissed(rooms.selectRoomError)}>Dismiss</button>
    </div>}</MessagingWorkspaceContext.Provider>;
}
