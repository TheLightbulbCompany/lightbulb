"use client";

import { createPortal } from "react-dom";
import { Fragment, useCallback, useEffect, useId, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { ArrowLeft, Check, Chrome, Circle, CircleAlert, FolderOpen, Info, Lightbulb, Link, ListChecks, Square } from "lucide-react";
import type { Components } from "react-markdown";
import { cardAnswersFor, LISTEN_MODE_LABELS, listenModeOptions, soleChannelOwner, useStatusLoaded, type CardReply, type CreateMessageRequest, type ListenMode, type MemberPrefsRequest, type MemberRef, type UpdateMessageRequest } from "@lightbulb/shared";
import { ToolActivityTrace } from "@/components/chat/ToolActivityTrace";
import { DelegateCard } from "./DelegateCard";
import { RoomSubagentStrip } from "./RoomSubagentStrip";
import { QuietDisclosure } from "@/components/chat/QuietDisclosure";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { DropdownMenuItem } from "@/components/ui/dropdown-menu";
import { Identity } from "@/components/teams/shared/components/Identity";
import { WelcomeIdea } from "@/components/ideas/IdeaCard";
import { MarkdownView } from "@/components/teams/shared/components/MarkdownView";
import { roomMarkdown as publicMarkdown } from "./roomMarkdown";
import { attachmentIdFromHref } from "@/components/teams/shared/lib/attachments";
import { hasBlockingShortcutDialog } from "@/components/teams/shared/lib/keyboardShortcuts";
import { safeHref } from "@/components/teams/shared/lib/safeHref";
import { handleShellLinkClick, shellUrlFromPaperclipPath } from "@/lib/shellNav";
import { cn } from "@/lib/utils";
import { roomUrlTransform } from "./markdown";
import { JOIN_LINK_COPIED, joinLinkNote } from "@/hooks/useCopyJoinLink";
import { copyText, messagePermalink, parseSessionPermalink } from "./messageLinks";
import { openBrowserPane, openInviteDialog, openPanel, openSessionPanel, openTaskPane, useThreadPane, useWorkspaceState } from "@/components/workspace/workspaceController";
import { capture } from "@/lib/analytics";
import { RunBrowserPreview } from "./BrowserRunPreview";
import { cardControls } from "./cardControls";
import { RoomComposer, type RoomComposerProps } from "./RoomComposer";
import { RoomDetailsPanel, type DetailsTab } from "./RoomDetailsPanel";
import { RoomHeader } from "./RoomHeader";
import { readRecentEmoji } from "./EmojiPicker";
import { ParticipantAvatar } from "./ParticipantAvatar";
import { ParticipantProfile } from "./ParticipantProfile";
import { FirstReplyChips, INCOMPLETE_FIRST_REPLIES } from "./FirstReplyChips";
import { RoomIntro } from "./RoomIntro";
import { OnboardingBand } from "./OnboardingBand";
import { RoomLimitBar } from "./RoomLimitBar";
import type { BillingAccount } from "@/hooks/useBilling";
import { RoomMessageItem } from "./RoomMessageItem";
import { TaskPane } from "@/components/tasks/TaskPane";
import { useTaskReply } from "@/components/tasks/taskReply";
import { TaskOverview } from "@/components/tasks/TaskOverview";
import { SpecialistDmBanner } from "./SpecialistDmBanner";
import { useRoomThreads } from "@/components/tasks/useRoomThreads";
import { useSuperAgent } from "@/hooks/useLiveEvents";
import { RoomMessageSearch } from "./RoomMessageSearch";
import { RoomTasks } from "./RoomTasks";
import { taskIdentifiers, useReferencedTasks } from "./MessageTaskCard";
import { RoomTimeline, StickToBottom } from "./RoomTimeline";
import { buildTimeline, collapseRuns, delegationIdOf, linkifyMentions, linkifySessionMentions, placeRuns, parseMentionHref, roomDisplayName, type LocalSession, type MessageDelegations, type MessagingClient, type RoomRunView, type RoomSubagents, type RoomSummary } from "@lightbulb/shared";
import { TypingReveal } from "./TypingReveal";
import { useAsyncAction } from "./useAsyncAction";
import type {
  PendingRoomMessage,
  DirectoryParticipant,
  RoomMessage,
  RoomParticipant,
  RoomState,
  WorkState,
} from "./types";
import "./messaging.css";

export interface RoomPanelProps {
  ownerScopeId: string;
  currentUserId: string;
  roomId: string;
  room?: RoomSummary;
  readSeqAtOpen?: number | null;
  targetMessageId?: string | null;
  state: RoomState;
  httpReady: boolean;
  liveConnected: boolean;
  participants: readonly RoomParticipant[];
  messages: readonly RoomMessage[];
  historyLoading?: boolean;
  historyComplete?: boolean;
  historyError?: boolean;
  historyErrorMessage?: string | null;
  onLoadHistory?: () => void;
  onLoadOlder?: () => void;
  pendingMessages?: readonly PendingRoomMessage[];
  typingMembers?: readonly { userId: string; displayName: string }[];
  runs?: readonly RoomRunView[];
  composer: RoomComposerProps;
  threadComposer?: { parentMessageId: string; composer: RoomComposerProps };
  /** Every session the viewer can open (the workspace drawer, offline and ended included). A
   *  referenced session resolves here, never from the composer's sendable targets. */
  sessions?: readonly LocalSession[];
  onSelectThread?: (parentMessageId: string | null) => void;
  onJumpToMessage?: (roomId: string, messageId: string) => void;
  selectedThreadId?: string | null;
  onSelectParticipant?: (participant: RoomParticipant) => void | Promise<void>;
  onRetry?: () => void;
  onListenModeChange?: (agentId: string, mode: ListenMode) => void;
  /** `/billing/account`; its `block_reason` pins the limit bar above the composer. */
  billing?: BillingAccount | null;
  onSetReaction?: (messageId: string, emoji: string, active: boolean) => Promise<void>;
  onUpdateMessage?: (messageId: string, request: UpdateMessageRequest) => Promise<void>;
  addMemberCandidates?: readonly DirectoryParticipant[];
  onAddMember?: (member: MemberRef) => Promise<void>;
  /** Copies the workspace join link (minting one if there is none): the door for someone not in the
   *  workspace yet. Any workspace member may copy it. */
  onCopyJoinLink?: () => Promise<void>;
  /** One-shot: open the member picker as soon as this room allows adding (the sidebar's ⋯ → Add people). */
  openAddPeople?: boolean;
  onAddPeopleOpened?: () => void;
  onRenameRoom?: (name: string) => Promise<void>;
  onRemoveMember?: (member: MemberRef) => Promise<void>;
  pinnedMessageIds?: readonly string[];
  onSetPin?: (messageId: string, pinned: boolean) => Promise<void>;
  onMarkUnreadFrom?: (fromSeq: number) => Promise<void>;
  onSetPrefs?: (prefs: MemberPrefsRequest) => Promise<void>;
  onSetTopic?: (topic: string | null) => Promise<void>;
  onLeaveRoom?: () => Promise<void>;
  onDeleteRoom?: () => Promise<void>;
  onSetArchived?: (archived: boolean) => Promise<void>;
  onAbortRun?: (runId: string) => void | Promise<unknown>;
  subagents?: RoomSubagents;
  /** Delegations of ended runs, keyed by the saved reply that replaced each run's card. */
  delegationsByMessage?: ReadonlyMap<string, MessageDelegations>;
  messagingClient?: MessagingClient;
  onSteerSubagent?: (agentId: string, subagentId: string, text: string) => Promise<unknown>;
  onStopSubagent?: (agentId: string, subagentId: string) => Promise<unknown>;
  onSubagentsVisible?: (visible: boolean) => void;
  onLinkToWork?: () => void;
  /** Super-agent: the one front agent (`@lightbulb`). A DM with any other agent shows the specialist banner. */
  frontAgentId?: string;
  /** The workspace runtime is still being set up: with the switch on, the viewer's unanswered message reads "Queued". */
  runtimeStarting?: boolean;
  onDownloadAttachment?: (roomId: string, attachmentId: string) => Promise<Blob>;
  onAttachmentUrl?: (roomId: string, attachmentId: string) => Promise<{ url: string }>;
  /** A card's click: posts the person's answer in reply to the card (MessagingWorkspaceContext builds it). */
  onCardReply?: (reply: CardReply) => Promise<void>;
}

/** How long a silent ask reads "Lightbulb is on it…" (a reply took ~2 min in the 5 Nov walk). */
const ON_IT_MS = 5 * 60_000;
const NO_PINNED_MESSAGES: readonly string[] = [];
const NO_TASK_IDS: readonly string[] = [];
const NO_RUNS: readonly RoomRunView[] = [];
const NO_SESSIONS: readonly LocalSession[] = [];

const DEFAULT_QUICK_REACTIONS = ["👍", "✅", "🎉", "👀"];

const workLabel: Record<WorkState, string> = {
  ready: "Ready", queued: "Queued", working: "Working", paused: "Paused", stopped: "Stopped",
  "needs-approval": "Needs approval", completed: "Completed", failed: "Failed", offline: "Offline", unknown: "Unknown",
};
function AgentResponse({ participant, selectId, onChange }: {
  participant: RoomParticipant;
  selectId: string;
  onChange?: (agentId: string, mode: ListenMode) => void;
}) {
  const mode = participant.listenMode ?? "mentions";
  if (!onChange) return <span className="text-ui text-muted-foreground">Response: {LISTEN_MODE_LABELS[mode]}</span>;
  return <><span className="text-ui text-muted-foreground">Response</span><select id={selectId} aria-label={`Response for ${participant.displayName}`} className="room-listen-mode" value={mode}
    onChange={(event) => onChange(participant.id, event.target.value as ListenMode)}>
    {listenModeOptions().map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}</select></>;
}

function WorkStatus({ state }: { state: WorkState }) {
  const tone = state === "completed" ? "text-ok" : state === "failed" ? "text-err"
    : state === "working" || state === "queued" ? "text-info" : state === "needs-approval" || state === "paused" ? "text-warn" : "text-muted-foreground";
  const Icon = state === "completed" ? Check : state === "failed" || state === "needs-approval" ? CircleAlert : Circle;
  return <span role="status" aria-label="Work status" className={`inline-flex items-center gap-1 text-ui ${tone}`}><Icon size={14} aria-hidden="true" />{workLabel[state]}</span>;
}

const roomMarkdown: Partial<Components> = {
  ...publicMarkdown,
  a: ({ href, children }) => {
    const mention = href ? parseMentionHref(href) : null;
    if (mention) return <span className="room-mention" data-kind={mention.kind}>{children}</span>;
    // Paperclip attachment links (`/api/attachments/{id}/content`) only
    // resolve on the issue-scoped Teams BFF — a room message has no owning
    // issue to fetch through, so render inert rather than a dead link to
    // this app's own host (mirrors roomMarkdown.tsx's own `a` override,
    // which this one otherwise fully replaces).
    if (attachmentIdFromHref(href)) return <span>{children}</span>;
    // This overrides publicMarkdown's own `a` (for mention/session-permalink
    // handling), so it needs its own paperclip-link resolution too — see
    // roomMarkdown.tsx for the shared rationale.
    const shellUrl = shellUrlFromPaperclipPath(href);
    if (shellUrl) return <a href={shellUrl} className="text-info underline underline-offset-4" onClick={event => {
      if (shellUrl.split("?")[0] === window.location.pathname) handleShellLinkClick(event, shellUrl);
    }}>{children}</a>;
    const safe = safeHref(href);
    const session = safe && typeof window !== "undefined" ? parseSessionPermalink(safe) : null;
    if (session) return <a href={safe!} className="text-info underline underline-offset-4" onClick={event => {
      if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      event.preventDefault();
      openSessionPanel(session.sessionId);
    }}>{children}</a>;
    return safe ? <a href={safe} target="_blank" rel="noopener noreferrer" className="text-info underline underline-offset-4">{children}</a> : <span>{children}</span>;
  },
};

const narrowQuery = "(max-width: 859px)";
const overlayQuery = "(min-width: 860px) and (max-width: 1179px)";

function subscribeNarrow(callback: () => void) {
  const media = window.matchMedia(narrowQuery);
  media.addEventListener("change", callback);
  return () => media.removeEventListener("change", callback);
}
const getNarrow = () => window.matchMedia(narrowQuery).matches;
const getServerNarrow = () => false;

/** The background subagents a run started outlive its card: they stay under the reply that replaced it. */
function savedDelegations(state: RoomSubagents | undefined, entry: MessageDelegations | undefined) {
  return state && entry ? <div className="room-message-delegations">{entry.delegationIds.map((delegationId) =>
    <DelegateCard key={delegationId} state={state} agentId={entry.agentId} delegationId={delegationId} agentLive={false} />)}</div> : null;
}

export function RoomPanel(props: RoomPanelProps) {
  return <RoomBoundary key={JSON.stringify([props.ownerScopeId, props.currentUserId, props.roomId])} {...props} />;
}

type Attempt = { observed: RoomComposerProps["sendState"]; state: RoomComposerProps["sendState"]; revision: number };

function RoomBoundary(props: RoomPanelProps) {
  const [attempts, setAttempts] = useState(() => new Map<string, Attempt>());

  function retainAttempt(composer: RoomComposerProps): RoomComposerProps {
    const key = composer.clientMessageId;
    const previous = attempts.get(key);
    const changed = composer.sendState !== "idle" && composer.sendState !== previous?.observed;
    const record = changed ? { observed: composer.sendState, state: composer.sendState, revision: previous?.revision ?? 0 } : previous;
    async function invoke(callback: RoomComposerProps["onSubmit"], request: Parameters<RoomComposerProps["onSubmit"]>[0]) {
      const revision = (record?.revision ?? 0) + 1;
      setAttempts((current) => new Map(current).set(key, { observed: composer.sendState, state: "sending", revision }));
      try {
        await callback(request);
      } catch (error) {
        setAttempts((current) => {
          const latest = current.get(key);
          if (!latest || latest.revision !== revision || latest.state === "persisted" || latest.state === "rejected") return current;
          return new Map(current).set(key, { ...latest, state: "uncertain" });
        });
        throw error;
      }
    }
    return {
      ...composer,
      sendState: record?.state ?? composer.sendState,
      members: composer.members.filter((target) => props.participants.some((member) => member.kind === target.kind && member.id === target.id)),
      onSubmit: (request) => invoke(composer.onSubmit, request),
      onRetry: composer.onRetry ? (request) => invoke(composer.onRetry!, request) : undefined,
    };
  }
  const composer = retainAttempt(props.composer);
  const threadComposer = props.threadComposer && { ...props.threadComposer, composer: retainAttempt(props.threadComposer.composer) };
  if (props.state === "loading") return <section className="room-panel room-blocked" aria-label="Loading conversation">
    <p role="status" aria-label="Room status">Loading conversation…</p><div className="room-skeleton" aria-hidden="true"><span /><span /><span /></div>
  </section>;
  const matchingIdentity = !!props.ownerScopeId && !!props.roomId && props.room?.roomId === props.roomId &&
    props.messages.every((message) => message.roomId === props.roomId) &&
    (props.pendingMessages ?? []).every((message) => message.roomId === props.roomId);
  if (!matchingIdentity || props.state === "access-removed" || props.state === "unavailable") return <section className="room-panel room-blocked" aria-label="Conversation unavailable">
    <p role="status" aria-label="Room status">{props.state === "access-removed" ? "Access removed. You can no longer view this conversation." : "This conversation is unavailable."}</p>
    {matchingIdentity && props.state === "unavailable" && props.onRetry && <Button variant="outline" onClick={props.onRetry}>Try again</Button>}
  </section>;
  return <RoomContent {...props} composer={composer} threadComposer={threadComposer} />;
}

function RoomContent({
  roomId,
  currentUserId,
  room,
  readSeqAtOpen,
  targetMessageId,
  state,
  httpReady,
  liveConnected,
  participants,
  messages,
  historyLoading,
  historyComplete,
  historyErrorMessage,
  onLoadHistory,
  onLoadOlder,
  pendingMessages = [],
  typingMembers = [],
  runs: allRuns = NO_RUNS,
  composer,
  threadComposer,
  sessions: visibleSessions = NO_SESSIONS,
  onSelectThread,
  onJumpToMessage,
  selectedThreadId,
  onSelectParticipant,
  onRetry,
  onListenModeChange,
  billing,
  onSetReaction,
  onUpdateMessage,
  addMemberCandidates = [],
  onAddMember,
  onCopyJoinLink,
  openAddPeople = false,
  onAddPeopleOpened,
  onRenameRoom,
  onRemoveMember,
  pinnedMessageIds = NO_PINNED_MESSAGES,
  onSetPin,
  onMarkUnreadFrom,
  onSetPrefs,
  onSetTopic,
  onLeaveRoom,
  onDeleteRoom,
  onSetArchived,
  onAbortRun,
  subagents,
  delegationsByMessage,
  messagingClient,
  onSteerSubagent,
  onStopSubagent,
  onSubagentsVisible,
  onLinkToWork,
  frontAgentId,
  runtimeStarting = false,
  onDownloadAttachment,
  onAttachmentUrl,
  onCardReply,
}: RoomPanelProps) {
  const id = useId();
  // token increments on every selection (even of the same result) so RoomTimeline
  // re-scrolls/re-highlights instead of treating a repeat click as already-handled.
  const searchTargetToken = useRef(0);
  // One indicator per agent: a follow-up is steered into the live run (the composer IS the
  // steer), and one that could not be waits for it -- its message says so instead of a
  // second "Working" card.
  const { runs, waiting } = useMemo(() => collapseRuns(allRuns), [allRuns]);
  // Keyed by value, like workingThreadKey: streamed tokens must not re-render history.
  const receiptKey = JSON.stringify([
    ...[...waiting].map(([messageId, agentId]) => [messageId, agentId, "waiting"]),
    ...allRuns.flatMap((run) => (run.steeredMessageIds ?? []).map((messageId) => [messageId, run.agentId, "seen"])),
  ]);
  // Super-agent, a new workspace: the viewer's latest message with nothing after it is admitted and kept until the
  // runtime is up (the Router waits for it), so it reads "Queued" rather than looking ignored.
  const lastTop = [...messages].reverse().find((message) => !message.threadRootMessageId && !message.deletedAt);
  const queuedId = useSuperAgent() && runtimeStarting && participants.some((p) => p.kind === "agent")
    && lastTop?.author.kind === "human" && lastTop.author.id === currentUserId
    ? lastTop.messageId : null;
  const receipts = useMemo(() => {
    const name = (agentId: string) => participants.find((member) => member.kind === "agent" && member.id === agentId)?.displayName ?? "The agent";
    const out = new Map((JSON.parse(receiptKey) as [string, string, string][]).map(([messageId, agentId, kind]) =>
      [messageId, kind === "seen" ? `${name(agentId)} saw this` : `${name(agentId)} will read this after the current reply`]));
    if (queuedId && !out.has(queuedId)) out.set(queuedId, "Queued · Lightbulb answers as soon as setup finishes");
    return out;
  }, [receiptKey, participants, queuedId]);
  // Stop is not instant (the backend asks the runtime first): say so until run_ended lands,
  // and give the button back if the request fails.
  const [stopping, setStopping] = useState<ReadonlySet<string>>(() => new Set());
  const stopRun = (runId: string) => {
    if (!onAbortRun) return;
    setStopping((current) => new Set(current).add(runId));
    void Promise.resolve(onAbortRun(runId)).catch(() => setStopping((current) => {
      const next = new Set(current);
      next.delete(runId);
      return next;
    }));
  };
  const [searchTarget, setSearchTarget] = useState<(Pick<RoomMessage, "messageId" | "threadRootMessageId"> & { token: number }) | null>(null);
  // RoomContent remounts per room (RoomBoundary is keyed by roomId), so this resets naturally on room change.
  const [sentSinceOpen, setSentSinceOpen] = useState(false);
  // Only suppress the "New" line once a send actually succeeds -- a rejected send must leave sentSinceOpen false.
  // Bumped when a send starts: the timeline takes the sender to the bottom before the pending row lands.
  const [sendToken, setSendToken] = useState(0);
  const [threadSendToken, setThreadSendToken] = useState(0);
  const markSent = (callback: RoomComposerProps["onSubmit"]) => async (request: CreateMessageRequest) => {
    setSendToken((token) => token + 1);
    await callback(request);
    setSentSinceOpen(true);
  };
  const [copied, setCopied] = useState<string | null>(null);
  const [copyError, setCopyError] = useState<string | null>(null);
  // The "Copied" label resets itself after 2 s; the timer must not outlive the component.
  const copiedTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => () => { if (copiedTimer.current) clearTimeout(copiedTimer.current); }, []);
  const [reactionPending, setReactionPending] = useState<string | null>(null);
  const [reactionError, setReactionError] = useState<string | null>(null);
  // Shared pending/error pair for the per-message menu actions (pin, mark unread) --
  // only one such action is ever in flight from a given message's menu at a time.
  const { pending: messageActionPending, error: messageActionError, run: runMessageAction } = useAsyncAction();
  // Computed once per mount/reaction-change (not per message render) -- reading
  // localStorage in every RoomMessageItem render was wasted work on a long list.
  const [quickReactions, setQuickReactions] = useState<readonly string[]>(() => {
    const recent = readRecentEmoji();
    return recent.length ? recent.slice(0, 4) : DEFAULT_QUICK_REACTIONS;
  });
  const [focusToken, setFocusToken] = useState(0);
  const [memberPickerOpen, setMemberPickerOpen] = useState(false);
  const [inviteNote, setInviteNote] = useState<string | null>(null);
  const [inviteBusy, setInviteBusy] = useState(false); // one mint at a time: a second POST replaces the first link
  const copyJoinLink = async () => {
    if (inviteBusy) return;
    setInviteBusy(true);
    setInviteNote(null);
    try {
      await onCopyJoinLink?.();
      setInviteNote(JOIN_LINK_COPIED);
    } catch (error) {
      setInviteNote(joinLinkNote(error));
    } finally {
      setInviteBusy(false);
    }
  };
  const [addingMembers, setAddingMembers] = useState(false);
  const [selectedMembers, setSelectedMembers] = useState<string[]>([]);
  const [memberQuery, setMemberQuery] = useState("");
  const [memberError, setMemberError] = useState<string>();
  const [management, setManagement] = useState<MemberRef | null>(null);
  const [managementPending, setManagementPending] = useState(false);
  const [managementError, setManagementError] = useState<string>();
  const managementTrigger = useRef<HTMLButtonElement | null>(null);
  const managementCancel = useRef<HTMLButtonElement>(null);
  const [contextCollapsed, setContextCollapsed] = useState(true);
  const [detailsTab, setDetailsTab] = useState<DetailsTab>("members");
  const [editTarget, setEditTarget] = useState<string | null>(null);
  const superAgent = useSuperAgent();
  // `from: "about"`: a thread opened from About's Threads tab goes back there, not to the conversation.
  const [localSelection, setSelection] = useState<{ kind: "thread" | "tasks" | "task"; id: string; from?: "about" } | null>(null);
  const selection = useMemo(() => selectedThreadId === undefined ? localSelection
    : selectedThreadId ? { kind: "thread" as const, id: selectedThreadId }
      : localSelection?.kind !== "thread" ? localSelection : null, [selectedThreadId, localSelection]);
  const loadedThreadHistory = useRef<string | null>(null);
  useEffect(() => {
    if (selection?.kind !== "thread" || historyComplete || historyLoading || !onLoadHistory) return;
    if (loadedThreadHistory.current === selection.id) return;
    loadedThreadHistory.current = selection.id;
    onLoadHistory();
  }, [selection, historyComplete, historyLoading, onLoadHistory]);
  const trigger = useRef<HTMLButtonElement | null>(null);
  const detailHeading = useRef<HTMLHeadingElement>(null);
  const contextHeading = useRef<HTMLHeadingElement>(null);
  const contextTrigger = useRef<HTMLButtonElement>(null);
  const contextWasOpen = useRef(false);
  const historyRef = useRef<HTMLDivElement>(null);
  const isNarrow = useSyncExternalStore(subscribeNarrow, getNarrow, getServerNarrow);
  // The shell's right pane: the room's tools open it (Browser, Files) beside the conversation.
  const { ready: inShell } = useWorkspaceState();
  const browserAvailable = useStatusLoaded();
  const visibleRoom = room?.roomId === roomId ? room : undefined;
  const actionable = httpReady && !!visibleRoom;
  // The welcome (seq 1) must be loaded: useRooms keeps only the newest 100, so a long agent-only run hides it.
  const showFirstReplies = Boolean(room?.isDefault) && !room?.archivedAt
    && messages[0]?.messageSeq === 1
    && !messages.some((m) => m.author.kind === "human")
    && pendingMessages.length === 0
    && actionable && !composer.disabled;
  const mainMessages = useMemo(() => messages.filter((message) => message.threadRootMessageId === null), [messages]);
  // One pass, as in the mobile timeline, instead of a full history scan per row.
  const repliesByRoot = useMemo(() => {
    const replies = new Map<string, { count: number; latest: RoomMessage; authors: Map<string, RoomMessage["author"]> }>();
    for (const message of messages) {
      if (!message.threadRootMessageId) continue;
      const previous = replies.get(message.threadRootMessageId);
      const authors = previous?.authors ?? new Map<string, RoomMessage["author"]>();
      authors.set(`${message.author.kind}:${message.author.id}`, message.author);
      replies.set(message.threadRootMessageId, {
        authors,
        count: (previous?.count ?? 0) + 1,
        latest: previous && previous.latest.messageSeq >= message.messageSeq ? previous.latest : message,
      });
    }
    return replies;
  }, [messages]);
  // Super-agent: the viewer's last ask to Lightbulb with nothing back yet (no later message, no agent reply under it,
  // no run on screen) reads "Lightbulb is on it…" until its reply or card lands. The Router posts no typing signal,
  // so this is the client's: an ask names Lightbulb, or Lightbulb listens to everything and no other person is here.
  // ponytail: gives up after ON_IT_MS of silence; a server "seen" signal would replace the timer.
  const front = superAgent && !runtimeStarting ? participants.find((p) => p.kind === "agent" && p.mentionHandle === "lightbulb") : undefined;
  const lastMain = mainMessages.findLast((message) => !message.deletedAt);
  const askPending = front && lastMain && lastMain.author.kind === "human" && lastMain.author.id === currentUserId
    && !runs.some((run) => run.state === "running")
    // A run for this ask that already ended (failed, stopped) shows its own outcome.
    && !allRuns.some((run) => run.triggerMessageId === lastMain.messageId)
    && ![...(repliesByRoot.get(lastMain.messageId)?.authors.values() ?? [])].some((author) => author.kind === "agent")
    && (lastMain.mentions.some((m) => m.kind === "agent" && m.id === front.id) || (front.listenMode === "active" && participants.filter((p) => p.kind === "human").length === 1))
    ? lastMain : undefined;
  const [onItExpired, setOnItExpired] = useState<string | null>(null);
  useEffect(() => {
    if (!askPending) return;
    const left = ON_IT_MS - (Date.now() - Date.parse(askPending.createdAt));
    const timer = window.setTimeout(() => setOnItExpired(askPending.messageId), Math.max(0, left));
    return () => window.clearTimeout(timer);
  }, [askPending]);
  const onIt = askPending && onItExpired !== askPending.messageId ? front : undefined;
  // Only membership changes invalidate history; streamed text/tool frames do not.
  const workingThreadKey = JSON.stringify(runs.filter(run => run.state === "running" && run.threadRootMessageId)
    .map(run => [run.threadRootMessageId, run.agentId]));
  const threadActivity = useMemo(() => {
    const result = new Map<string, { participants: Map<string, DirectoryParticipant>; working: Set<string> }>();
    const entry = (root: string) => {
      if (!result.has(root)) result.set(root, { participants: new Map(), working: new Set() });
      return result.get(root)!;
    };
    const identity = (kind: "human" | "agent", id: string, name?: string) =>
      participants.find(member => member.kind === kind && member.id === id)
        ?? { kind, id, displayName: name || "Unknown author", mentionHandle: null };
    for (const [root, replies] of repliesByRoot) {
      for (const [key, author] of replies.authors) {
        if (author.kind !== "system") entry(root).participants.set(key, identity(author.kind, author.id, author.displayName));
      }
    }
    for (const [root, agentId] of JSON.parse(workingThreadKey) as [string, string][]) {
      const agent = identity("agent", agentId, "Agent");
      entry(root).participants.set(`agent:${agentId}`, agent);
      entry(root).working.add(agent.displayName);
    }
    return result;
  }, [repliesByRoot, participants, workingThreadKey]);
  const newLineSeq = readSeqAtOpen ?? null;
  const ownSeqs = mainMessages.filter((message) => message.author.kind === "human" && message.author.id === currentUserId).map((message) => message.messageSeq);
  const ownLastSentSeq = ownSeqs.length ? Math.max(...ownSeqs) : null;
  // Only a main-list (non-thread) search result with no thread open is a target
  // for RoomTimeline -- a thread-scoped result is handled by the effect below.
  const activeSearchTarget = searchTarget && !searchTarget.threadRootMessageId && !selection ? searchTarget : null;
  const parentMessage = selection?.kind === "thread" ? messages.find((message) => message.messageId === selection.id) : undefined;
  const showTasks = selection?.kind === "tasks" && !!onLinkToWork;
  const taskSelection = superAgent && selection?.kind === "task" ? selection.id : null;
  const showDetail = !!(parentMessage || showTasks || taskSelection);
  // An open request pane is steered from this composer ("Replying to: …"); null while the composer isn't shown.
  const taskReply = useTaskReply(superAgent && !visibleRoom?.feed && !visibleRoom?.archivedAt ? roomId : null, composer,
    () => setFocusToken((t) => t + 1));
  // The About button's dot: anything in this room that needs the viewer (Home's Needs you cut; no request while off).
  const { waitingOnMe } = useRoomThreads(superAgent ? roomId : null);
  const showContext = showDetail || !contextCollapsed;
  const activeRuns = runs.filter((run) => run.state === "running");
  // Any author: a human naming ISO-14 references it as much as an agent does.
  // ponytail: loaded pages only; a task named only in unloaded history is missing until scrolled in.
  const taskIds = useMemo(() => [...new Set(messages.flatMap((message) =>
    message.deletedAt || message.author.kind === "system" ? [] : taskIdentifiers(message.text)))], [messages]);
  const roomTasks = useReferencedTasks(onLinkToWork ? taskIds : NO_TASK_IDS);
  const viewer = participants.find((participant) => participant.kind === "human" && participant.id === currentUserId);
  const canAddMembers = !!visibleRoom && !!onAddMember && !visibleRoom.shared;
  const isRoomAdmin = viewer?.role === "owner" || viewer?.role === "admin";
  const canManageMembers = visibleRoom?.kind === "channel" && isRoomAdmin;
  const canEditRoom = visibleRoom?.kind !== "dm" && isRoomAdmin;
  // A group chat's creator holds no powers there, so anyone may leave one. A channel owner
  // leaving hands the channel to another person in it (the backend picks them) -- but
  // alone in it, they can only delete it: the Leave dialog explains that and offers Delete.
  const ownerAlone = soleChannelOwner(visibleRoom && { kind: visibleRoom.kind, members: participants }, currentUserId);
  const canLeaveRoom = !visibleRoom?.shared
    && (visibleRoom?.kind === "group-dm" || (visibleRoom?.kind === "channel" && !ownerAlone));
  const ownerCannotLeave = !visibleRoom?.shared && ownerAlone;
  // A group chat has no admins: any member archives and unarchives it.
  const canUnarchive = !!onSetArchived && (visibleRoom?.kind === "group-dm" || isRoomAdmin);
  const roomNoun = visibleRoom?.kind === "group-dm" ? "group chat" : "channel";
  const normalizedMemberQuery = memberQuery.trim().toLocaleLowerCase();
  const memberMatches = (name: string) => !normalizedMemberQuery || name.toLocaleLowerCase().includes(normalizedMemberQuery);
  const filteredAddMemberCandidates = addMemberCandidates.filter((candidate) => memberMatches(candidate.displayName));
  const filteredCurrentMembers = participants.filter((participant) => memberMatches(participant.displayName));
  const removalTarget = management
    ? participants.find((member) => member.kind === management.kind && member.id === management.id && member.role !== "owner")
    : undefined;

  function openManagement(action: MemberRef, button: HTMLButtonElement) {
    managementTrigger.current = button;
    setManagementError(undefined);
    setManagement(action);
  }

  async function saveManagement() {
    if (!actionable || managementPending) return;
    if (!canManageMembers || !onRemoveMember || !removalTarget) return;
    setManagementPending(true);
    setManagementError(undefined);
    try {
      await onRemoveMember({ kind: removalTarget.kind, id: removalTarget.id });
      setManagement(null);
    } catch (error) {
      setManagementError(error instanceof Error ? error.message : "The channel could not be updated. Try again.");
    } finally {
      setManagementPending(false);
    }
  }

  async function addSelectedMembers() {
    if (!onAddMember || selectedMembers.length === 0) return;
    setAddingMembers(true);
    setMemberError(undefined);
    try {
      for (const key of selectedMembers) {
        const member = addMemberCandidates.find((candidate) => `${candidate.kind}:${candidate.id}` === key);
        if (member) await onAddMember({ kind: member.kind, id: member.id });
      }
      setSelectedMembers([]);
    } catch (error) {
      setMemberError(error instanceof Error ? error.message : "Members could not be added.");
    } finally {
      setAddingMembers(false);
    }
  }

  useEffect(() => {
    if (selection) detailHeading.current?.focus({ preventScroll: true });
    else trigger.current?.focus({ preventScroll: true });
  }, [selection]);

  useEffect(() => {
    if (!searchTarget?.threadRootMessageId) return;
    if (selection?.kind !== "thread" || selection.id !== searchTarget.threadRootMessageId) return;
    const target = document.getElementById(`${id}-thread-${searchTarget.messageId}`);
    target?.scrollIntoView({ block: "center" });
    target?.focus({ preventScroll: true });
  }, [id, searchTarget, selection]);

  useEffect(() => {
    // Opening only steals focus into the panel on narrow layouts (it docks
    // beside the conversation at wider widths, so nothing forces focus off
    // whatever the viewer was already doing). Closing always has to put
    // focus somewhere, though: the <aside> unmounts unconditionally at every
    // width, and with no explicit target here the browser drops focus to
    // <body> -- so the trigger gets it back regardless of viewport.
    if (!contextCollapsed && isNarrow && !selection) contextHeading.current?.focus({ preventScroll: true });
    else if (contextCollapsed && contextWasOpen.current && !selection) contextTrigger.current?.focus({ preventScroll: true });
    contextWasOpen.current = !contextCollapsed;
  }, [contextCollapsed, isNarrow, selection]);

  const closeDetail = useCallback(() => {
    if (selection?.kind === "thread") onSelectThread?.(null);
    setSelection(null);
  }, [onSelectThread, selection?.kind]);
  // In the shell, the side column (thread, tasks, room details) is the right pane's content;
  // when something else takes the pane, the column folds.
  const counterparts = participants.filter((participant) => participant.kind !== "human" || participant.id !== currentUserId);
  const roomName = roomDisplayName(visibleRoom, counterparts);
  const sideTitle = `${showTasks ? "Tasks" : showDetail ? "Thread" : "About"} · ${roomName}`;
  const sideHost = useThreadPane(showContext, () => { closeDetail(); setContextCollapsed(true); }, sideTitle);

  const openMemberPicker = useCallback(() => {
    closeDetail();
    setDetailsTab("members");
    setMemberError(undefined);
    setMemberQuery("");
    setMemberPickerOpen(true);
    setContextCollapsed(false);
  }, [closeDetail]);
  useEffect(() => {
    if (!openAddPeople || !canAddMembers) return;
    openMemberPicker();
    onAddPeopleOpened?.();
  }, [openAddPeople, canAddMembers, openMemberPicker, onAddPeopleOpened]);

  const closeOverlay = useCallback(() => {
    if (showDetail && !isNarrow && !sideHost && window.matchMedia(overlayQuery).matches) closeDetail();
  }, [closeDetail, isNarrow, showDetail, sideHost]);

  useEffect(() => {
    if (!showDetail) return;
    // An open dialog owns Escape — dismissing the overlay underneath it as
    // well would close two things on one keypress. A Radix popper (menu,
    // emoji picker) is portalled out of this subtree, so its own
    // Escape reaches this window listener too and needs the same exemption.
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !hasBlockingShortcutDialog()
        && !(event.target as Element | null)?.closest?.("[data-radix-popper-content-wrapper]")) closeOverlay();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [closeOverlay, showDetail]);

  const openDetail = useCallback((kind: "thread" | "tasks" | "task", targetId: string, button: HTMLButtonElement, fromThread = false) => {
    if (!fromThread) trigger.current = button;
    onSelectThread?.(kind === "thread" ? targetId : null);
    setSelection({ kind, id: targetId });
  }, [onSelectThread]);

  const openTask = useCallback((taskId: string) => {
    // A message's task card opens the workspace pane instead (openTaskPane: pane= in the URL, as from Work).
    // From About, its button stays the way back: the row that was pressed leaves the DOM with the tab.
    const fromAbout = !contextCollapsed;
    if (document.activeElement instanceof HTMLButtonElement && !fromAbout) trigger.current = document.activeElement;
    onSelectThread?.(null);
    setSelection({ kind: "task", id: taskId, ...(fromAbout ? { from: "about" as const } : {}) });
  }, [onSelectThread, contextCollapsed]);
  const isAgentId = useCallback((memberId: string) => participants.some((p) => p.kind === "agent" && p.id === memberId), [participants]);

  const toggleReaction = useCallback(async (message: RoomMessage, emoji: string) => {
    if (!onSetReaction || !actionable || reactionPending !== null) return;
    setReactionPending(message.messageId);
    setReactionError(null);
    try { await onSetReaction(message.messageId, emoji, !message.reactions?.[emoji]?.includes(currentUserId)); }
    catch { setReactionError(message.messageId); }
    finally {
      setReactionPending(null);
      const recent = readRecentEmoji();
      setQuickReactions(recent.length ? recent.slice(0, 4) : DEFAULT_QUICK_REACTIONS);
    }
  }, [onSetReaction, actionable, reactionPending, currentUserId]);

  const welcomeRoom = Boolean(room?.isDefault) && inShell;
  const revealStarter = Boolean(room?.isDefault) && !messages.some((m) => m.author.kind === "human");
  // One pass per message list, so a keystroke (which re-renders the panel) re-scans nothing.
  const { cardsEnabled, repliesEnabled } = cardControls(room);
  const cardAnswers = useMemo(() => cardAnswersFor(messages), [messages]);
  const renderMessage = useCallback((message: RoomMessage, inThread = false, collapsed = false) => {
    const participant = participants.find((member) => member.kind === message.author.kind && member.id === message.author.id);
    const mentionsMe = message.mentions.some((mention) => mention.kind === "human" && mention.id === currentUserId);
    const replies = inThread ? undefined : repliesByRoot.get(message.messageId);
    const body = (text: string) => <RoomMessageItem key={message.messageId} id={`${id}-${inThread ? "thread" : "main"}-${message.messageId}`}
      message={message} text={linkifySessionMentions(linkifyMentions(text, message.mentions, participants), message, visibleSessions)} participant={participant} currentUserId={currentUserId} actionable={actionable} inThread={inThread} collapsed={collapsed}
      startEditing={!inThread && editTarget === message.messageId}
      onEditingClosed={() => setEditTarget((current) => current === message.messageId ? null : current)}
      replyCount={replies?.count ?? 0} lastReplyAt={replies?.latest.createdAt}
      threadParticipants={[...(threadActivity.get(message.messageId)?.participants.values() ?? [])]}
      workingNames={[...(threadActivity.get(message.messageId)?.working ?? [])]}
      receipt={receipts.get(message.messageId)}
      highlighted={inThread && searchTarget?.messageId === message.messageId} mentionsViewer={mentionsMe} markdown={roomMarkdown} urlTransform={roomUrlTransform}
      quickReactions={quickReactions} reactionPending={reactionPending !== null} reactionError={reactionError === message.messageId}
      onToggleReaction={onSetReaction && !message.deletedAt ? (emoji) => void toggleReaction(message, emoji) : undefined}
      onOpenThread={(button) => openDetail("thread", message.messageId, button)}
      onSelectParticipant={onSelectParticipant} onUpdateMessage={onUpdateMessage} onDownloadAttachment={onDownloadAttachment} onAttachmentUrl={onAttachmentUrl}
      cardsEnabled={cardsEnabled} onCardReply={repliesEnabled && participant ? onCardReply : undefined} cardAnswer={cardAnswers.get(message.messageId) ?? null}
      sessions={visibleSessions} onOpenSession={openSessionPanel}
      onOpenTask={superAgent ? openTaskPane : undefined} isAgentId={superAgent ? isAgentId : undefined}
      menuExtras={!message.deletedAt && (onSetPin || (onMarkUnreadFrom && !inThread)) ? <>
        {/* disabled/aria-busy reflect messageActionPending -- shared across every message's
            menu here, same one-pending-pair-per-surface shape as useAsyncAction's doc comment.
            The menu itself still closes on select (Radix default); reopening it while a
            request is in flight is what surfaces the disabled/pending state, so the request's
            eventual error stays visible (never trapped behind an aria-hidden open menu). */}
        {onSetPin && <DropdownMenuItem disabled={messageActionPending} aria-busy={messageActionPending} onSelect={() => void runMessageAction(
          () => onSetPin(message.messageId, !pinnedMessageIds.includes(message.messageId)),
          "The pin could not be saved.",
        )}>{messageActionPending ? (pinnedMessageIds.includes(message.messageId) ? "Unpinning…" : "Pinning…")
          : (pinnedMessageIds.includes(message.messageId) ? "Unpin from conversation" : "Pin to conversation")}</DropdownMenuItem>}
        {onMarkUnreadFrom && !inThread && <DropdownMenuItem disabled={messageActionPending} aria-busy={messageActionPending} onSelect={() => void runMessageAction(
          () => onMarkUnreadFrom(message.messageSeq),
          "The conversation could not be marked unread.",
        )}>{messageActionPending ? "Marking unread…" : "Mark unread from here"}</DropdownMenuItem>}
      </> : undefined}
      toolbarExtras={!message.deletedAt ? <>
        <Button type="button" variant="ghost" size="icon-sm" aria-label="Copy link"
          title={copied === message.messageId ? "Copied" : "Copy link"}
          onClick={async () => {
            const ok = await copyText(messagePermalink(roomId, message.messageId));
            setCopied(ok ? message.messageId : null);
            setCopyError(ok ? null : message.messageId);
            if (ok) {
              if (copiedTimer.current) clearTimeout(copiedTimer.current);
              copiedTimer.current = setTimeout(() => setCopied((current) => current === message.messageId ? null : current), 2000);
            }
          }}><Link aria-hidden="true" /></Button>
        {copyError === message.messageId && <p role="alert" className="text-ui text-err">Could not copy the link.</p>}
      </> : undefined} />;
    // The seeded CEO welcome streams in until a human has written (client-side, cosmetic).
    if (revealStarter && !inThread && message.messageSeq === 1 && message.author.kind === "agent") {
      return <TypingReveal key={message.messageId} text={message.text} playKey={message.roomId}>{body}</TypingReveal>;
    }
    return body(message.text);
  }, [revealStarter, participants, currentUserId, repliesByRoot, threadActivity, receipts, id, actionable, editTarget, searchTarget,
    quickReactions, reactionPending, reactionError, onSetReaction, toggleReaction, openDetail, superAgent, isAgentId,
    onSelectParticipant, onUpdateMessage, onDownloadAttachment, onAttachmentUrl, onCardReply, cardsEnabled, repliesEnabled, cardAnswers, onSetPin, onMarkUnreadFrom,
    messageActionPending, runMessageAction, pinnedMessageIds, copied, copyError, roomId, visibleSessions]);
  // A run that ended without a saved reply (stopped, failed, no reply) stays where it happened,
  // after the message it followed; only live runs ride the tail below the newest message.
  // Keyed by value, like receiptKey: streamed frames of a live run must not re-render history.
  const mainRuns = runs.filter((run) => !run.threadRootMessageId);
  const endedRuns = mainRuns.filter((run) => run.state !== "running");
  const settledKey = endedRuns.map((run) => `${run.runId}:${run.state}:${run.afterSeq}:${run.text.length}:${run.tools.length}`).join("|");
  const settledRuns = useMemo(() => placeRuns(endedRuns, mainMessages).after,
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [settledKey, mainMessages]);
  // Subagent events re-render history only while some saved reply carries cards that read them.
  const delegationState = delegationsByMessage?.size ? subagents : undefined;
  const renderMainMessage = useCallback((message: RoomMessage, collapsed: boolean) => <>
    {renderMessage(message, false, collapsed)}
    {/* The CEO's welcome carries the top idea as a proposal and points at the 💡 (spec 2026-10-14 §4.4). */}
    {welcomeRoom && message.messageSeq === 1 && message.author.kind === "agent" && <WelcomeIdea />}
    {savedDelegations(delegationState, delegationsByMessage?.get(message.messageId))}
    {settledRuns.get(message.messageId)?.map(renderRun)}
  </>,
  // An ended run's card reads nothing renderRun closes over that settledRuns does not carry.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  [renderMessage, settledRuns, delegationState, delegationsByMessage, welcomeRoom]);

  function renderRun(run: RoomRunView) {
    const agent = participants.find((participant) => participant.kind === "agent" && participant.id === run.agentId);
    const agentName = agent?.displayName ?? "Agent";
    if (run.state !== "running" && !run.text) {
      const label = run.state === "completed" ? `${agentName} finished · no reply`
        : run.state === "aborted" ? `${agentName} stopped${run.forcedStop ? " (the agent may finish its current step)" : ""}`
          : `${agentName}'s run failed`;
      return <div key={`${run.roomId}:${run.agentId}:${run.runId}`} className={cn("room-run-line", run.state === "failed" ? "text-err" : "text-muted-foreground")} role="status">
        <Circle size={12} aria-hidden="true" />{label}
        {/* No "What it did" wrapper: the trace folds itself to one line, and a
            disclosure around a disclosure put that line behind two clicks. */}
        {run.tools.length > 0 && <div className="room-run-trace"><ToolActivityTrace calls={run.tools} /></div>}
        {subagents && run.tools.filter((t) => t.name === "delegate_task").map((t) => {
          const delegationId = delegationIdOf(t.result);
          return delegationId ? <DelegateCard key={t.id} state={subagents} agentId={run.agentId} delegationId={delegationId} agentLive={run.state === "running"} /> : null;
        })}
      </div>;
    }
    const workState = run.state === "running" ? "working" : run.state === "aborted" ? "stopped" : run.state;
    return <article key={`${run.roomId}:${run.agentId}:${run.runId}`} className="room-message room-live-run" aria-label={`Live run from ${agentName}`}
      data-reload-blocker={run.state === "running" || undefined}>
      <Identity name={agentName} avatarOnly size="default" /><div className="room-message-content"><div className="room-message-byline"><span className="font-semibold">{agentName}</span><WorkStatus state={workState} />
          {/* Labelled, at the far right of the header row: a bare square on its own line read as an empty
              checkbox (prod 2026-09-25). "Stopping…" holds until the run's own run_ended. */}
          {run.state === "running" && onAbortRun && <Button type="button" variant="ghost" size="sm"
            className="ml-auto h-6 shrink-0 gap-1 self-center px-2 text-caption text-muted-foreground hover:text-foreground [&_svg:not([class*='size-'])]:size-3"
            disabled={!actionable || stopping.has(run.runId)}
            aria-label={stopping.has(run.runId) ? `Stopping ${agentName}…` : `Stop ${agentName}`}
            title={stopping.has(run.runId) ? `Stopping ${agentName}…` : `Stop ${agentName}`}
            onClick={() => stopRun(run.runId)}><Square aria-hidden="true" className="fill-current" />{stopping.has(run.runId) ? "Stopping…" : "Stop"}</Button>}</div>
        {run.thinking && <QuietDisclosure label="Thinking" className="mb-1"><p className="room-wrap text-muted-foreground">{run.thinking}</p></QuietDisclosure>}
        {run.text && <MarkdownView skipHtml components={roomMarkdown} urlTransform={roomUrlTransform}>{run.text}</MarkdownView>}
        {run.tools.length > 0 && <ToolActivityTrace calls={run.tools} />}
        {subagents && run.tools.filter((t) => t.name === "delegate_task").map((t) => {
          const delegationId = delegationIdOf(t.result);
          return delegationId ? <DelegateCard key={t.id} state={subagents} agentId={run.agentId} delegationId={delegationId} agentLive={run.state === "running"} /> : null;
        })}
        {run.state === "running" && <RunBrowserPreview agentName={agentName} run={run} />}
        {run.forcedStop && <p className="text-ui text-muted-foreground" role="status">Stopped (the agent may finish its current step)</p>}
      </div>
    </article>;
  }

  const dockSide = (column: React.ReactNode) => sideHost ? createPortal(column, sideHost) : column;
  return <section
    className={cn("room-panel", showContext && !sideHost && "room-has-context", showDetail ? "room-has-detail" : "room-has-default-context")} aria-label={roomName}>
    <div className="room-conversation" hidden={showContext && isNarrow && !sideHost} onClickCapture={closeOverlay} onFocusCapture={closeOverlay}>
      <RoomHeader room={visibleRoom} roomName={roomName} counterparts={counterparts} participants={participants} participantCount={participants.length}
        canEditRoom={canEditRoom} canLeave={canLeaveRoom} ownerCannotLeave={ownerCannotLeave} ownerHandsOff={canLeaveRoom && visibleRoom?.kind === "channel" && viewer?.role === "owner"} actionable={actionable} pinnedCount={pinnedMessageIds.length}
        search={<RoomMessageSearch roomId={roomId}
          onSelect={(hit) => {
            const root = hit.threadRootMessageId;
            // Otherwise the [selection] effect's else-branch focuses this stale trigger
            // (e.g. a "Reply in thread" button) once selection clears below, racing
            // RoomTimeline's own focus of the newly targeted main message.
            trigger.current = null;
            // Same race against the [contextCollapsed] effect below: if the room
            // details panel happens to be open, closing it here must not steal
            // focus back to the Details tab once RoomTimeline focuses this result.
            contextWasOpen.current = false;
            onSelectThread?.(root);
            setContextCollapsed(true);
            if (root) {
              setSelection({ kind: "thread", id: root });
              setSearchTarget({ messageId: hit.messageId, threadRootMessageId: root, token: ++searchTargetToken.current });
            } else {
              setSelection(null);
              // Server hits may not be loaded yet: the permalink target pages history until it is.
              // A fresh token makes picking an already-targeted message scroll again.
              setSearchTarget({ messageId: hit.messageId, threadRootMessageId: null, token: ++searchTargetToken.current });
              onJumpToMessage?.(roomId, hit.messageId);
            }
          }} />}
        onOpenDetails={(tab) => { closeDetail(); if (tab) setDetailsTab(tab); setContextCollapsed(false); }}
        onQuickAddPeople={canManageMembers && !visibleRoom?.shared ? openMemberPicker : undefined}
        onAddPeople={canAddMembers ? openMemberPicker : undefined}
        onSetPrefs={onSetPrefs} onRenameRoom={onRenameRoom} onSetTopic={onSetTopic} onLeaveRoom={onLeaveRoom}
        onSetArchived={onSetArchived} onDeleteRoom={onDeleteRoom}
        // One labelled About button (the room's threads, members, pins, files, links and details); the rest of the
        // room's tools sit in the header's ··· menu.
        views={<Button ref={contextTrigger} type="button" variant="ghost" size="sm" className="room-view-about relative"
          aria-expanded={!contextCollapsed} aria-label={waitingOnMe > 0 ? `About, ${waitingOnMe} need${waitingOnMe === 1 ? "s" : ""} you` : "About"}
          onClick={() => {
            closeDetail();
            if (contextCollapsed && superAgent) { capture("overview_opened", { source: "room", count: waitingOnMe }); setDetailsTab("threads"); }
            setContextCollapsed(!contextCollapsed);
          }}>
          <Info aria-hidden="true" />About
          {waitingOnMe > 0 && <span aria-hidden="true" className="absolute right-1 top-1 size-1.5 rounded-full bg-warn" />}
        </Button>}
        menuItems={<>
          {/* The Paperclip tasks this room names. With the switch on they are older work: offered only when there are any. */}
          {onLinkToWork && (!superAgent || roomTasks.length > 0) && <DropdownMenuItem onSelect={() => { onSelectThread?.(null); setSelection({ kind: "tasks", id: "tasks" }); }}>
            <ListChecks aria-hidden="true" />{superAgent ? `Older tasks · ${roomTasks.length}` : roomTasks.length > 0 ? `Tasks · ${roomTasks.length}` : "Tasks"}</DropdownMenuItem>}
          {/* Ideas: from an agent's DM, that agent's ideas come first. */}
          {inShell && <DropdownMenuItem onSelect={() => { capture("ideas_opened"); openPanel("ideas", composer.focusAgentId ? { agent: composer.focusAgentId } : {}, { pushUrl: true }); }}>
            <Lightbulb aria-hidden="true" />Ideas</DropdownMenuItem>}
          {/* The shared Browser is offered once a status body has landed (it can only show its
              empty state before), the agent's files always. Both open the right pane. */}
          {inShell && browserAvailable && <DropdownMenuItem onSelect={() => { capture("browser_view_opened"); openBrowserPane(); }}>
            <Chrome aria-hidden="true" />Browser</DropdownMenuItem>}
          {inShell && <DropdownMenuItem onSelect={() => { capture("file_browser_opened"); openPanel("file-viewer", composer.focusAgentId ? { agent: composer.focusAgentId } : {}, { pushUrl: true }); }}>
            <FolderOpen aria-hidden="true" />Agent files</DropdownMenuItem>}
        </>} />
      {superAgent && frontAgentId && visibleRoom?.kind === "dm" && (() => {
        // Only once the front agent is known: an unresolved roster must not call every agent a specialist.
        const agent = counterparts.length === 1 && counterparts[0].kind === "agent" && counterparts[0].id !== frontAgentId ? counterparts[0] : null;
        return agent ? <SpecialistDmBanner agentName={agent.displayName} handle={agent.mentionHandle} frontAgentId={frontAgentId} /> : null;
      })()}
      {(state === "reconnecting" || !liveConnected) && <div className="room-state"><p role="status" aria-label="Room status">Reconnecting live updates. You can keep sending messages.</p>
        {onRetry && <Button variant="outline" onClick={onRetry}>Check for updates</Button>}</div>}
      <div ref={historyRef} className="contents">
        <RoomTimeline roomId={roomId} messages={mainMessages} readSeq={sentSinceOpen ? null : newLineSeq} ownLastSentSeq={ownLastSentSeq} sendToken={sendToken}
          historyComplete={historyComplete} historyLoading={historyLoading} historyError={historyErrorMessage} onLoadOlder={onLoadOlder}
          targetMessageId={targetMessageId ?? (activeSearchTarget ? activeSearchTarget.messageId : null)}
          targetToken={activeSearchTarget && activeSearchTarget.messageId === (targetMessageId ?? activeSearchTarget.messageId) ? activeSearchTarget.token : undefined}
          renderMessage={renderMainMessage}
          leading={historyComplete ? <>
            <RoomIntro room={visibleRoom} roomName={roomName} participants={participants} />
            {/* Starter channel only, identified by the backend's `isDefault` —
                the room is renameable, so "general" is not the test. */}
            {visibleRoom?.isDefault && <OnboardingBand participants={participants}
              humanSpoke={pendingMessages.length > 0 || mainMessages.some((message) => message.author.kind === "human")} />}
          </> : null}
          empty={!historyComplete ? <p className="room-empty">Load older messages to see the start of this conversation.</p> : null}>
          {pendingMessages.filter((message) => message.threadRootMessageId === null).map((message) => <article key={message.clientMessageId} className="room-message opacity-60" aria-label={`Pending message from ${viewer?.displayName ?? "You"}`}>
            <Identity name={viewer?.displayName ?? "You"} avatarOnly size="default" /><div className="room-message-content"><div className="room-message-byline"><span className="font-semibold">{viewer?.displayName ?? "You"}</span><span className="text-ui text-muted-foreground">Sending…</span></div><MarkdownView skipHtml components={roomMarkdown} urlTransform={roomUrlTransform}>{linkifyMentions(message.text, message.mentions, participants)}</MarkdownView></div>
          </article>)}
          {placeRuns(mainRuns, mainMessages).tail.map(renderRun)}
        </RoomTimeline>
      </div>
      <p className="room-typing room-typing-live text-ui text-muted-foreground" role="status">{typingMembers.length > 0 ? `${typingMembers.slice(0, 3).map((member) => member.displayName).join(", ")}${typingMembers.length > 3 ? " and others" : ""} ${typingMembers.length === 1 ? "is" : "are"} typing…` : onIt ? `${onIt.displayName} is on it…` : ""}</p>
      {copied && <p role="status" aria-label="Link copied" className="sr-only">Link copied</p>}
      {messageActionError && <p role="alert" className="room-typing text-ui text-err">{messageActionError}</p>}
      {!visibleRoom?.archivedAt && <RoomLimitBar account={billing} />}
      {showFirstReplies && <FirstReplyChips onPick={(text) => { if (composer.text.trim() === "") composer.onTextChange(text); setFocusToken((t) => t + 1); }} />}
      {visibleRoom?.feed ? <div className="room-composer room-archived" role="status">
        <p className="text-ui text-muted-foreground">Only agents post to the feed. Reply to a post in its thread.</p>
      </div> : visibleRoom?.archivedAt ? <div className="room-composer room-archived" role="status">
        <p className="text-ui text-muted-foreground">
          {canUnarchive ? `This ${roomNoun} is archived. Unarchive it to post.` : `This ${roomNoun} is archived.`}
        </p>
        {canUnarchive && <Button type="button" size="sm" variant="outline" disabled={!actionable || messageActionPending}
          onClick={() => void runMessageAction(() => onSetArchived!(false), `The ${roomNoun} could not be unarchived.`)}>Unarchive {roomNoun}</Button>}
      </div> : <>{subagents && messagingClient && onSteerSubagent && onStopSubagent && onSubagentsVisible && <RoomSubagentStrip state={subagents} roomId={roomId}
          agentName={(agentId) => participants.find((m) => m.kind === "agent" && m.id === agentId)?.displayName ?? "Agent"}
          client={messagingClient} onSteer={onSteerSubagent} onStop={onStopSubagent} onVisible={onSubagentsVisible} />}
        {taskReply?.chip}
        <RoomComposer {...composer} focusToken={focusToken} placeholderDrafts={showFirstReplies ? INCOMPLETE_FIRST_REPLIES : undefined}
        onSubmit={markSent(composer.onSubmit)}
        onRetry={composer.onRetry ? markSent(composer.onRetry) : undefined}
        onEditLast={onUpdateMessage ? () => {
          const own = mainMessages.filter((message) => message.author.kind === "human" && message.author.id === currentUserId && !message.deletedAt);
          setEditTarget(own.length ? own[own.length - 1].messageId : null);
        } : undefined}
        disabled={!actionable || composer.disabled}
        dropTargetRef={historyRef} {...taskReply?.composer} /></>}
    </div>
    {dockSide(<>{showContext && !showDetail && <aside className="room-context" aria-label="About"
      onKeyDown={(event) => { if (event.key === "Escape") { event.stopPropagation(); setContextCollapsed(true); } }}>
      <header className="room-context-header"><Button variant="ghost" aria-label="Back to conversation" onClick={() => setContextCollapsed(true)}><ArrowLeft aria-hidden="true" /></Button><h3 ref={contextHeading} tabIndex={-1} className="font-semibold">About</h3></header>
      <RoomDetailsPanel room={visibleRoom} roomName={roomName} tab={detailsTab} onSelectTab={setDetailsTab}
        threads={superAgent ? <TaskOverview scope={{ kind: "room", roomId }} viewerId={currentUserId} onOpen={openTask} /> : undefined}
        participants={participants} actionable={actionable}
        messages={messages} pinnedMessageIds={pinnedMessageIds} activeRuns={activeRuns}
        onUnpin={onSetPin ? (messageId) => onSetPin(messageId, false) : undefined}
        onLeaveRoom={onLeaveRoom && canLeaveRoom ? onLeaveRoom : undefined} ownerCannotLeave={ownerCannotLeave}
        // Reuses the shipped search scroll/highlight path rather than adding a second one.
        onOpenMessage={(messageId) => {
          // Jumping here always closes this very panel -- skip the generic
          // [contextCollapsed] close-focus-restore (below) so it doesn't fight
          // RoomTimeline's own focus of the message just jumped to.
          contextWasOpen.current = false;
          setContextCollapsed(true);
          setSearchTarget({ messageId, threadRootMessageId: null, token: ++searchTargetToken.current });
        }}
        members={<section aria-labelledby={`${id}-context-members`}>
          <div className="room-members-heading"><h4 id={`${id}-context-members`} className="font-semibold">Members</h4>
            {canAddMembers && <Button type="button" variant="outline" aria-expanded={memberPickerOpen} onClick={() => {
              if (!memberPickerOpen) { setMemberError(undefined); setMemberQuery(""); }
              setMemberPickerOpen(!memberPickerOpen);
            }}>Add members</Button>}
          </div>
          {visibleRoom?.isDefault && participants.filter((p) => p.kind === "human").length === 1 && (() => {
            const agents = participants.filter((p) => p.kind === "agent").length;
            return <p className="text-ui text-muted-foreground">1 person · {superAgent ? "" : `${agents} AI ${agents === 1 ? "employee" : "employees"} · `}
              <button type="button" className="underline" onClick={openInviteDialog}>Invite people</button></p>;
          })()}
          {onCopyJoinLink && <p className="text-ui text-muted-foreground">Someone not in the workspace yet?{" "}
            <button type="button" className="underline" disabled={inviteBusy} onClick={() => void copyJoinLink()}>Copy the workspace join link</button> and send it to them.
            {inviteNote && <span role="status"> {inviteNote}</span>}</p>}
          {/* Super-agent: people and Lightbulb; specialists and their listen modes are plumbing (the Team page has them). */}
          <ul>{(superAgent ? participants.filter((p) => p.kind === "human" || p.mentionHandle === "lightbulb") : participants).map((participant) => <li key={`${participant.kind}:${participant.id}`} className="room-member flex-wrap">
            <ParticipantAvatar participant={participant} size="sm" />
            <ParticipantProfile participant={participant} currentUserId={currentUserId} onMessage={onSelectParticipant} disabled={!actionable} className="flex-1 min-w-24" />
            {!(superAgent && participant.kind === "agent") && <span className="text-ui text-muted-foreground">{participant.kind === "agent" ? "Agent" : "Person"}</span>}
            {canManageMembers && onRemoveMember && participant.role !== "owner" && !(visibleRoom?.shared && participant.kind === "human") && !(superAgent && participant.kind === "agent") && <Button type="button" variant="ghost" size="sm" disabled={!actionable}
              aria-label={`Remove ${participant.displayName}`} onClick={(event) => openManagement({ kind: participant.kind, id: participant.id }, event.currentTarget)}>Remove</Button>}
            {participant.kind === "agent" && !superAgent && <AgentResponse participant={participant} selectId={`${id}-context-${participant.id}-mode`} onChange={onListenModeChange} />}
          </li>)}</ul>
          {canAddMembers && memberPickerOpen && <div className="room-member-picker">
            <label className="sr-only" htmlFor={`${id}-member-search`}>Search people</label>
            <input id={`${id}-member-search`} type="search" className="room-input" placeholder="Search people" value={memberQuery}
              onChange={(event) => setMemberQuery(event.target.value)} />
            {filteredCurrentMembers.length > 0 && <section aria-label="Current members">
              <p className="text-ui font-medium">Current members</p>
              <ul>{filteredCurrentMembers.map((member) => <li key={`${member.kind}:${member.id}`} className="room-member-choice text-muted-foreground">
                <ParticipantAvatar participant={member} size="sm" /><span>{member.displayName}</span><span className="text-ui">Already here</span>
              </li>)}</ul>
            </section>}
            {addMemberCandidates.length === 0 ? <p className="text-ui text-muted-foreground">Everyone in your workspace is already here.</p>
              : <><fieldset disabled={addingMembers}><legend className="text-ui font-medium">Eligible people</legend>
                {filteredAddMemberCandidates.length === 0
                  ? <p className="text-ui text-muted-foreground">No eligible people match your search.</p>
                  : filteredAddMemberCandidates.map((candidate) => {
                    const key = `${candidate.kind}:${candidate.id}`;
                    return <label key={key} className="room-member-choice"><input type="checkbox" aria-label={candidate.displayName} checked={selectedMembers.includes(key)}
                      onChange={(event) => setSelectedMembers((current) => event.target.checked ? [...current, key] : current.filter((item) => item !== key))} />
                      <ParticipantAvatar participant={candidate} size="sm" /><span>{candidate.displayName}</span><span className="text-ui text-muted-foreground">{candidate.kind === "agent" ? "Agent" : "Person"}</span></label>;
                  })}
              </fieldset>
              <Button type="button" disabled={selectedMembers.length === 0 || addingMembers} onClick={() => void addSelectedMembers()}>{addingMembers ? "Adding…" : "Add selected"}</Button></>}
            {memberError && <p role="alert" className="text-ui text-err">{memberError}</p>}
          </div>}
        </section>} />
    </aside>}
    {showTasks && <aside className="room-context" aria-label="Tasks" onKeyDown={(event) => { if (event.key === "Escape") { event.stopPropagation(); closeDetail(); } }}>
      <header className="room-context-header"><Button variant="ghost" aria-label="Back to conversation" onClick={closeDetail}><ArrowLeft aria-hidden="true" />Back</Button>
        <h3 ref={detailHeading} tabIndex={-1} className="font-semibold">Tasks</h3></header>
      <div className="room-context-body"><RoomTasks tasks={roomTasks} onOpenAll={onLinkToWork!} /></div>
    </aside>}
    {taskSelection && <TaskPane key={taskSelection} taskId={taskSelection} viewerId={currentUserId} headingRef={detailHeading}
      {...(localSelection?.from === "about" ? { onClose: closeDetail, backLabel: "Back to requests" } : { onClose: closeDetail })} />}
    {showDetail && !showTasks && !taskSelection && <aside className="room-context" aria-label="Thread" onKeyDown={(event) => { if (event.key === "Escape") { event.stopPropagation(); closeDetail(); } }}>
      <header className="room-context-header"><Button variant="ghost" aria-label="Back to conversation" onClick={closeDetail}><ArrowLeft aria-hidden="true" />Back</Button>
        <h3 ref={detailHeading} tabIndex={-1} className="font-semibold room-wrap">Thread</h3></header>
      <StickToBottom className="room-context-body" pinToken={threadSendToken}>{parentMessage ? <>{renderMessage(parentMessage, true)}
        {(() => {
          const replies = messages.filter((message) => message.threadRootMessageId === parentMessage.messageId);
          const placed = placeRuns(runs.filter((run) => run.threadRootMessageId === parentMessage.messageId), replies);
          return <div className="room-thread-replies" aria-label="Thread replies">{buildTimeline(replies, { readSeq: null }).map((item) => item.kind === "message"
            ? <Fragment key={item.key}>{renderMessage(item.message, true, item.collapsed)}
              {savedDelegations(delegationState, delegationsByMessage?.get(item.message.messageId))}
              {placed.after.get(item.message.messageId)?.map(renderRun)}</Fragment> : null)}
          {placed.tail.map(renderRun)}
          </div>;
        })()}
      </> : null}</StickToBottom>
      {parentMessage && threadComposer?.parentMessageId === parentMessage.messageId && <RoomComposer {...threadComposer.composer} label="Reply to thread"
        onSubmit={(request) => { setThreadSendToken((token) => token + 1); return threadComposer.composer.onSubmit(request); }} disabled={!actionable || !!visibleRoom?.archivedAt || threadComposer.composer.disabled} />}
    </aside>}</>)}
    <Dialog open={management !== null && canManageMembers} onOpenChange={(open) => { if (!open && !managementPending) setManagement(null); }}>
      <DialogContent className="max-w-[calc(100%-2rem)] sm:max-w-md max-h-[calc(100dvh-2rem)] overflow-y-auto"
        onOpenAutoFocus={(event) => { event.preventDefault(); managementCancel.current?.focus(); }}
        onCloseAutoFocus={(event) => { event.preventDefault(); (managementTrigger.current?.isConnected ? managementTrigger.current : contextHeading.current)?.focus(); }}>
        <DialogHeader><DialogTitle>Remove member</DialogTitle>
          <DialogDescription className="break-words">{`Remove ${removalTarget?.displayName ?? "this member"} from ${roomName}? They will lose access to this channel and its messages.`}</DialogDescription>
        </DialogHeader>
        <form className="grid gap-4" onSubmit={(event) => { event.preventDefault(); void saveManagement(); }}>
          {managementError && <p role="alert" className="text-ui text-err break-words">{managementError}</p>}
          <DialogFooter className="gap-2"><Button ref={managementCancel} type="button" variant="outline" disabled={managementPending} onClick={() => setManagement(null)}>Cancel</Button>
            <Button type="submit" variant="destructive" disabled={!actionable || managementPending || !removalTarget}>
              {managementPending ? "Removing…" : "Remove member"}
            </Button></DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  </section>;
}
