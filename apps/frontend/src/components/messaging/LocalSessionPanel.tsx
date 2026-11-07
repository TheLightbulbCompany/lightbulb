"use client";

import { useCallback, useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useAuth } from "@clerk/nextjs";
import { isDesktopApp } from "@/lib/desktopNotify";
import { capture } from "@/lib/analytics";
import { Info, MessageSquare, Send } from "lucide-react";
import {
  ApiError, audienceLine, canControlSession, endedWhy, useSessionResume, mainEvents, pendingApproval, pendingQuestion, questionKeys, receiptChip, sendBlockedReason, sessionAgentsLabel, sessionChanges, sessionFooter, sessionPrLabel, sessionLabel, sessionLive, sessionPlan, sessionStatus, sessionTitle, unavailableReason,
  CLI_ONLY, conversationOnly, pickupRequestLine, pickupStatus, sessionScreenNotice, useHistorySource, useLocalSessionHistory, usePendingPrompt, useSessionCommand, useSessionJoinRequest, useSessionOutbox, watchOnlyLine,
  type LocalSession, type SessionControlClient, type SessionQuestion, type SessionSource,
} from "@lightbulb/shared";
import { Button } from "@/components/ui/button";
import { NativeSessionTranscript, ReceiptChip, type Person } from "./NativeSessionTranscript";
import { SessionApprovalCard } from "./SessionControls";
import { SessionQuestionCard } from "./SessionQuestion";
import { SessionTerminalView, type SessionFrames } from "./SessionTerminalView";
import { SessionBrowserTab, showsBrowserTab } from "./SessionBrowserTab";
import { useOptionalLiveEvents } from "@/hooks/useLiveEvents";
import { useLocalAudience, useLocalTerminal } from "./localTerminal";
import { toBase64 } from "./sessionInput";
import { SessionDetails, type SessionDetailsTab } from "./SessionDetails";
import { canPickUp, SessionPickup } from "./SessionPickup";
import { timeAgo } from "@/components/teams/shared/lib/timeAgo";
import type { PickupClient } from "./useSessionPickup";
import { ResumeDelivery, SessionResume } from "./SessionResume";
import { useApi } from "@/lib/api";

/** Older history pages read per session for the plan and changed-files summaries: 20 pages is 1,000 events,
 *  under the history window's 2,000-row cap, so reading back never evicts the newest events. A longer
 *  session's summary covers its last 1,000 events; a complete one needs a server-side summary. */
const MAX_OLDER_PAGES = 20;
/** How long an ended held session may be without its kept screen before the screen counts as gone: Claude's
 *  SessionEnd hook ends the session before the provider exits, and the Mac reports the kept screen on a later
 *  heartbeat (11.5 s in prod, desktop 0.4.625). */
const SCREEN_DUE_MS = 20_000;
const NO_EVENTS: never[] = [];
/** Between two keys of a question's answer: the picker reads one write as one key ("21" is not 2 then 1). */
const KEY_GAP_MS = 350;
/** The input route's refusals (`session_screen.type_into`) that it raises before anything is sent to the
 *  Mac, so the key was never typed and may be sent again. Every other failure leaves unknown whether it
 *  landed: LOCAL_SESSION_OFFLINE is also what a sent frame whose ack was lost reads as, and a timeout or a
 *  lost connection has no answer at all. */
const KEY_NOT_SENT = new Set(["LOCAL_SESSION_FORBIDDEN", "LOCAL_SESSION_NOT_FOUND", "LOCAL_SESSION_UNSUPPORTED", "RATE_LIMITED"]);
const keyNotSent = (e: unknown) => e instanceof ApiError && KEY_NOT_SENT.has(String((e.body as { code?: unknown } | null)?.code));
/** Distance from the bottom, in px, still counted as "at the bottom" (the phone's value). */
const BOTTOM_SLACK = 80;
/** Within this many pixels of the top, older output is fetched. */
const SCROLLBACK_SLACK = 240;

type Decide = (sessionId: string, userId: string, decision: "allow" | "decline", requestId?: string) => Promise<unknown>;
type JoinDecider = { decideLocalSessionJoin?: Decide; decideLocalSessionPickup?: Decide };
type Client = SessionSource["client"] & SessionControlClient & PickupClient & Parameters<typeof SessionDetails>[0]["client"] & JoinDecider & { watchLocalSession?: (sessionId: string) => Promise<unknown>; sendLocalSessionInput?: (sessionId: string, data: string, epoch: number, size?: { cols: number; rows: number; claim: boolean }) => Promise<unknown> };

/**
 * A shared session: its status on one line and ONE body (owner rulings 2026-10-27): the live terminal
 * while there is one, for everyone, with no conversation stacked under it and no approval card over it
 * (its own prompt is the surface). Where there is none (not started, paused, Mac offline, a search
 * hit) the body is the conversation; an ended one says so, over its kept final screen while there is one.
 * A session that runs inside an app has no terminal: its body is its conversation,
 * as on the phone. What the session is running, what it changed and who may do
 * what with it live behind Details.
 */
export function LocalSessionPanel({ session: supplied, ownerName, own, client, revision, onAccessLost, participants = [], onMessageOwner, nameOf, onEvent, focusSeq, openedConversation }: {
  openedConversation?: { current: string | null };
  /** A turn to open the conversation at (a session search hit): its row is scrolled to and marked. */
  focusSeq?: number;
  /** Live events, for watching a running session's terminal from another machine. */
  onEvent?: SessionSource["onEvent"];
  session: LocalSession; ownerName: string; own: boolean; client: Client;
  /** A workspace member's name for the audit lines; defaults to the participants list. */
  nameOf?: (userId: string) => string;
  revision: number; onAccessLost: () => void;
  /** Workspace humans and agents, so a teammate- or agent-sent prompt reads by name, not "Teammate". */
  participants?: readonly Person[];
  onMessageOwner?: () => void;
}) {
  // Not compiled: it reads the clock and a ref during render on purpose (the screen-due timer, the kept
  // terminal), which the React Compiler would memoize stale.
  "use no memo";
  const { userId, orgId } = useAuth();
  const resume = useSessionResume(client, supplied, {
    own, userId, ownerName, uuid: () => crypto.randomUUID(),
    nameOf: who => supplied.pendingJoinNames?.[who] ?? nameOf?.(who) ?? participants.find(p => p.id === who)?.displayName ?? "A teammate",
    autoOpen: own && isDesktopApp() && !focusSeq && !conversationOnly(supplied),
    openedConversation,
  });
  // A resumed run may be absent from the sidebar's current listing. The direct
  // read is authoritative for the whole panel, including terminal attachment.
  const session = resume.session;
  // The history feeds the plan, the changed files, the pending prompt and the approval reason, and an app
  // session's conversation. The relay throttles hints with no trailing edge, so a quiet poll while busy keeps those current.
  const busy = session.status === "busy";
  const source = useHistorySource(client, session, revision);
  const history = useLocalSessionHistory(client, source.sessionId, source.latestEventSeq, revision, onAccessLost, { pollMs: busy ? 5000 : 0 });
  const [text, setText] = useState("");
  const [details, setDetails] = useState<SessionDetailsTab | null>(null);
  // Nothing scrolls the history any more, so read back through older pages for the plan and changed files. Bounded per session while Details is closed; with Details open the read runs to the start, so Changes and the plan are complete.
  const olderPages = useRef({ id: session.sessionId, n: 0 });
  const scroller = useRef<HTMLDivElement>(null);
  const content = useRef<HTMLDivElement>(null);
  const atBottomRef = useRef(true);
  const anchor = useRef<{ height: number; top: number } | null>(null);
  useEffect(() => {
    if (olderPages.current.id !== source.sessionId) olderPages.current = { id: source.sessionId, n: 0 };
    if (!history.hasOlder || history.loading || history.error || olderPages.current.n >= MAX_OLDER_PAGES) return;
    olderPages.current.n += 1;
    const node = scroller.current;
    if (node) anchor.current = { height: node.scrollHeight, top: node.scrollTop };
    void history.load("older");
  }, [history, source.sessionId, details]);
  const [tab, setTab] = useState<SessionDetailsTab>("about");
  const slot = useRef<HTMLDivElement>(null);
  // Running on this Mac: the real terminal replaces the session view (rulings 2026-10-03 §2).
  // A live terminal only where the Mac has one (rulings 2026-10-04): held, or a Claude background
  // session through `claude attach`.
  const hasTerminal = !!session.terminal;
  // Inside an app there is never one: the conversation is the body, whatever the session's status.
  const convo = conversationOnly(session);
  const running = session.status !== "ended" && session.status !== "offline";
  // An ended held session keeps its final screen while the owner's app runs (spec 2026-10-07 §5.4). Everyone, its
  // owner at that Mac included, watches it through the relay: the app's own panel closes with the provider.
  const endedScreen = session.status === "ended" && session.screenAvailable === true && hasTerminal;
  // An offline session still probes this Mac's holder: "offline" is the relay's view (sharing off, a reconnect), not the Mac's.
  // Paused: nothing of it leaves its Mac, so no picture here; it reads Paused at once, not a frozen live one.
  const paused = session.shared === false;
  // A search hit opens the conversation at its turn, even on a live session, until the viewer goes back.
  const [leftHit, setLeftHit] = useState<number | null>(null);
  const hit = !!focusSeq && leftHit !== focusSeq;
  // The local holder is authoritative even before relay capability metadata catches up.
  const local = useLocalTerminal(session.status === "ended" || convo ? null : { sessionId: session.sessionId }, slot, details === null);
  // Until then the panel does not say the screen is gone; the timer redraws it when the wait is over.
  // (`wasHeld`: the server sends no `terminal` for an ended session until its screen is reported.)
  const screenDueAt = session.status === "ended" && (hasTerminal || session.wasHeld === true) && !endedScreen && session.endedAt ? Date.parse(session.endedAt) + SCREEN_DUE_MS : 0;
  const [, redraw] = useState(0);
  useEffect(() => {
    const left = screenDueAt - Date.now();
    if (!(left > 0)) return;
    const timer = setTimeout(() => redraw(n => n + 1), left);
    return () => clearTimeout(timer);
  }, [screenDueAt]);
  // eslint-disable-next-line react-hooks/purity -- not compiled ("use no memo"); the timer above redraws
  const screenDue = Date.now() < screenDueAt;
  // The terminal view follows its last row, which in a kept picture can be a blank part of the screen: its
  // first scroll goes back to the top left instead, and after that the reader's scrolling is their own.
  const placed = useRef("");
  const openAtTop = (event: { target: EventTarget }) => {
    const box = event.target;
    if (placed.current === session.sessionId || !(box instanceof HTMLElement) || !box.classList.contains("session-terminal-scroll")) return;
    placed.current = session.sessionId;
    box.scrollTop = 0; box.scrollLeft = 0;
  };
  // The owner's own terminal on this Mac says who watches as the web terminal view does (`audienceLine`).
  const audience = audienceLine(useLocalAudience(local === "local", session.sessionId, session.generation));
  const canWatch = local === "none" && !!onEvent && !!client.watchLocalSession;
  const liveEvents = useOptionalLiveEvents();
  const framesChannel = liveEvents?.sessionFramesChannel(session.sessionId) ?? null;
  const frames = useMemo<SessionFrames | undefined>(() => liveEvents && framesChannel
    ? { channel: framesChannel, claim: liveEvents.claimSessionFrames, liveChannels: liveEvents.liveChannels }
    : undefined, [liveEvents, framesChannel]);
  // A viewer who was watching when the session ended keeps that terminal, with its last picture, until the
  // kept screen is reported or the wait is over: the server sends no `terminal` in between, and a terminal
  // taken away there came back empty, on "Connecting…" (prod 2026-10-07).
  const watched = useRef("");
  const keptWatching = canWatch && screenDue && watched.current === session.sessionId;
  const watchRemote = (canWatch && hasTerminal && !hit && ((running && !paused) || endedScreen)) || keptWatching;
  useEffect(() => { watched.current = watchRemote ? session.sessionId : ""; }, [watchRemote, session.sessionId]);
  // Terminal | Browser while the session has a browser connection open.
  // The session the Browser tab was picked for: another session, or no tab, reads as Terminal.
  const [browserFor, setBrowserFor] = useState<string | null>(null);
  const browserTab = showsBrowserTab(session, local === "none" && !!onEvent);
  if (!browserTab && browserFor !== null) setBrowserFor(null);
  const showBrowser = browserTab && browserFor === session.sessionId;
  // Keys typed into the live terminal from outside it (a question's answer).
  const keys = useRef<((data: string) => Promise<unknown>) | null>(null);
  const [screenLive, setScreenLive] = useState(false);
  // False once the terminal is read-only for this viewer (the Mac refused input): the message box is the way to send then.
  const [termCanType, setTermCanType] = useState(true);
  const liveTerminal = watchRemote && screenLive;
  const notStarted = running && !hasTerminal;
  // No live terminal here: the conversation, as on the phone (owner rulings 2026-10-11, 2026-10-27). Never
  // both: the transcript under a live terminal read as the session twice (prod 2026-10-27). Workspace
  // members only by construction: /s never mounts this panel and the events route 404s outside the workspace.
  // Keep saved messages readable after the terminal closes; a search hit can also open them.
  const savedConversation = session.status === "ended" && !endedScreen && !screenDue && history.events.length > 0;
  const conversation = !convo && !watchRemote && (notStarted || session.status === "offline" || paused || hit || savedConversation);
  const toBottom = useCallback(() => { const node = scroller.current; if (node) node.scrollTop = node.scrollHeight; }, []);
  useLayoutEffect(() => {
    const node = scroller.current;
    if (!node || !anchor.current) return;
    node.scrollTop = node.scrollHeight - anchor.current.height + anchor.current.top;
    anchor.current = null;
  }, [history.events]);
  useEffect(() => {
    const node = content.current;
    if (!node || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => { if (atBottomRef.current) toBottom(); });
    observer.observe(node);
    return () => observer.disconnect();
  }, [toBottom, conversation]);
  // The hit's turn: the last prompt or reply row at or before it, once older pages hold it.
  const scrolledTo = useRef<number | undefined>(undefined);
  useEffect(() => {
    if (!focusSeq || scrolledTo.current === focusSeq || history.error) return;
    const box = document.querySelector('[aria-label="Native session history"], [aria-label="Session conversation"]');
    const rows = [...(box?.querySelectorAll<HTMLElement>("[data-seq]") ?? [])];
    const first = rows.length ? Number(rows[0].dataset.seq) : Infinity;
    if (first > focusSeq && history.hasOlder) {
      if (!history.loading) void history.load("older");
      return;
    }
    const row = rows.filter(node => Number(node.dataset.seq) <= focusSeq).at(-1) ?? rows[0];
    if (!row) return;
    scrolledTo.current = focusSeq;
    atBottomRef.current = false;
    box?.querySelector("[data-search-hit]")?.removeAttribute("data-search-hit");
    row.setAttribute("data-search-hit", "");
    row.scrollIntoView({ block: "center" });
  }, [focusSeq, history]);
  const onScroll = () => {
    const node = scroller.current;
    if (!node) return;
    atBottomRef.current = node.scrollHeight - node.scrollTop - node.clientHeight <= BOTTOM_SLACK;
    // An older page can be in flight while the reader keeps moving. Preserve where
    // they moved to, rather than restoring the position captured when the read began.
    if (anchor.current) anchor.current.top = node.scrollTop;
    if (history.loading) return;
    if (node.scrollTop < SCROLLBACK_SLACK && history.hasOlder) {
      anchor.current = { height: node.scrollHeight, top: node.scrollTop };
      void history.load("older");
    } else if (atBottomRef.current && history.hasNewer) void history.load("newer");
  };
  const id = useId();
  const people = useMemo(() => [{ kind: "human" as const, id: session.humanUserId, displayName: ownerName }, ...participants], [session.humanUserId, ownerName, participants]);
  const agentName = useCallback((agentId: string) => people.find(p => p.id === agentId)?.displayName ?? "Agent", [people]);
  // An ask from the public page (T17) names someone not in the workspace yet: the server sends their label.
  const asked = session.pendingJoinNames;
  const personName = useCallback((who: string) => asked?.[who] ?? nameOf?.(who) ?? people.find(p => p.id === who)?.displayName ?? "A teammate", [nameOf, people, asked]);
  const command = useSessionCommand(client, session, { uuid: () => crypto.randomUUID(), onAccessLost, onSent: () => setText(""), agentName, allowInterrupt: own || !!session.teamControl, allowOffline: true, allowEnd: own });
  // Two clicks, as in the Mac's own terminal bar: a stray one never ends a session.
  const [endArmed, setEndArmed] = useState(false);
  useEffect(() => {
    if (!endArmed) return;
    const timer = setTimeout(() => setEndArmed(false), 3000);
    return () => clearTimeout(timer);
  }, [endArmed]);
  const outbox = useSessionOutbox(client, session.sessionId,
    `${session.status}:${session.status === "offline" ? revision : 0}:${command.attempt?.request.requestId}:${command.receipt?.state}`,
    session.status !== "offline" && session.status !== "ended");
  useEffect(() => {
    const updated = outbox.commands.find(receipt => receipt.requestId === command.attempt?.request.requestId);
    if (updated) command.syncReceipt(updated);
  }, [outbox.commands, command.attempt?.request.requestId, command.syncReceipt]);
  // A subagent's rows are tagged and read in its own transcript, never in the session's.
  const main = useMemo(() => mainEvents(history.events), [history.events]);
  const savedOutbox = outbox.commands.filter(receipt => receipt.requestId !== command.attempt?.request.requestId);
  const queuedOutbox = savedOutbox.filter(receipt => receipt.state !== "applied");
  const pendingText = usePendingPrompt(command.attempt, main);
  const blocked = sendBlockedReason(session);
  const waitingOn = session.status === "waiting-approval" ? pendingApproval(main) ?? session.waitingFor ?? "" : null;
  const question = session.status === "waiting-approval" && session.provider === "claude" ? pendingQuestion(main) : null;
  const target = sessionLabel(session);
  // One row per session a person opens: whether anyone looks at a teammate's session is the
  // first question the sharing feature has to answer. No title, no path, no text.
  useEffect(() => {
    capture("session_opened", { own, provider: session.provider, workspace_id: orgId, session_id: session.sessionId, origin: "web" });
  }, [session.sessionId, own, session.provider, orgId]);
  const control = canControlSession(session, own, userId);
  // The live terminal takes this viewer's keys (it stays mounted, hidden, under the Conversation tab).
  const canAnswer = liveTerminal && termCanType && control && session.shared !== false && !!client.sendLocalSessionInput;
  // A live terminal is the only input: no message box under it. A viewer who cannot type keeps the watch-only line.
  const terminalLive = local === "local" || liveTerminal;
  const terminalOnScreen = terminalLive && !showBrowser;
  const askPickup = useSessionJoinRequest(client, session, "pickup");
  const glyph = session.provider === "claude" ? "❯" : "›";
  const plan = useMemo(() => sessionPlan(main), [main]);
  const changes = useMemo(() => sessionChanges(main), [main]);
  const footer = sessionFooter(session);
  // The status line has room for the repository's own name and its branch; Details has the rest.
  const repo = session.pickup?.hasCode ? session.pickup.repo?.split("/").pop() : undefined;
  const where = repo ? [repo, session.pickup?.branch].filter(Boolean).join(" on ") : session.cwdName ?? "";
  // Details opens from the button and from the status line's links; focus returns to whichever it was.
  const opener = useRef<HTMLElement | null>(null);
  const show = (next: SessionDetailsTab) => {
    opener.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    setTab(next); setDetails(next);
  };

  const messageOwner = !own && onMessageOwner
    ? <Button type="button" variant="outline" size="sm" onClick={onMessageOwner}><MessageSquare aria-hidden="true" />Message {ownerName}</Button> : null;
  // Git's count from the session's Mac when it sends one (spec 2026-10-26 (f) gap 7): it sees edits made
  // by shell, checkout and codegen that the transcript's edit events miss. Else the events' count.
  const files = session.changes?.files ?? changes.length;
  const added = session.changes?.additions ?? changes.reduce((n, file) => n + file.added, 0);
  const removed = session.changes?.deletions ?? changes.reduce((n, file) => n + file.removed, 0);
  const agents = sessionAgentsLabel(session);
  const pr = sessionPrLabel(session);
  const done = plan?.filter(step => step.status === "completed").length ?? 0;

  // The sent-but-not-echoed prompt and the queued rows, so a follow-up stays visible; under an app session's conversation.
  // ponytail: the conversation is the events already read for the summaries (the last 1,000); add a read-back on scroll when someone needs more.
  const feedback = <>
          <NativeSessionTranscript provider={session.provider} events={convo || conversation ? history.events : NO_EVENTS} busy={(convo || conversation) && busy && !history.hasNewer} ownerName={ownerName} participants={people}
            status={session.status} pending={pendingText && !history.hasNewer ? { text: pendingText, chip: command.chip,
              onRetry: command.receipt?.state === "rejected" ? command.retry : undefined,
              onCancel: command.receipt?.state === "rejected" ? command.dismiss : command.canCancel ? () => void command.cancel() : undefined } : null}
            outbox={queuedOutbox.map(receipt => ({
              requestId: receipt.requestId, text: receipt.text ?? "",
              chip: receiptChip({ request: { requestId: receipt.requestId, expectedGeneration: receipt.expectedGeneration, kind: "send", text: receipt.text ?? "" }, state: "received", receipt }, agentName),
              onCancel: outbox.canCancel && receipt.state === "pending" ? () => void outbox.cancel(receipt.requestId) : undefined,
            }))} />
    {queuedOutbox.some(receipt => receipt.state === "pending" || receipt.state === "dispatched" || receipt.state === "unknown") && <Button type="button" variant="outline" size="sm" onClick={() => void outbox.refresh()}>Check queued delivery</Button>}
  </>;

  // Not a held session: its first events take up to a minute to reach the server, so zero events is not "nothing was sent".
  const endedEmpty = session.provider === "claude" && session.latestEventSeq === 0 && !session.controlUnavailableReason && !session.wasHeld;
  // An app session that ended with nothing in it: the ended panel alone, not an empty conversation.
  // (Its first events can reach the server up to a minute after the end: the history read, not the cached row, has the last word.)
  const convoEmpty = convo && session.status === "ended" && session.latestEventSeq === 0 && history.events.length === 0;
  const endedAt = session.endedAt ? <time dateTime={session.endedAt} title={new Date(session.endedAt).toLocaleString()}>{timeAgo(session.endedAt)}</time> : null;
  const why = endedWhy(session, own ? "your Mac" : `${ownerName}'s Mac`);
  const endedPanel = <div className="session-state-panel" role="status">
    <p className="session-state-title">{why ? `Ended · ${why}` : screenDue && !endedEmpty ? "Ended just now" : endedAt ? <>Ended {endedAt}</> : "Ended"}</p>
    {(endedEmpty || !screenDue) && <p>{endedEmpty || convoEmpty ? "This session ended before anything was sent." : "The screen is gone: this session's terminal closed."}</p>}
  </div>;
  const endedLine = <p className="session-ended-line" role="status">{why ? `Ended · ${why}` : endedAt ? <>Ended · {endedAt}</> : "Ended"}</p>;
  // A paused session is never heartbeated (nothing of it leaves its Mac), so it reads offline whether or not its Mac is on.
  const offlinePanel = paused ? <div className="session-state-panel" data-compact={convo || conversation || undefined} role="status">
    <p className="session-state-title">Paused</p>
    <p>{own ? "Sharing is paused, so nothing of this session leaves your Mac. Share it again from its terminal." : `${ownerName} paused sharing this session.`}</p>
  </div> : <div className="session-state-panel" data-compact={convo || conversation || undefined} role="status">
    <p className="session-state-title">{own ? "Your Mac is offline" : `${ownerName}'s Mac is offline`}</p>
    {session.lastEventAt && <p>Last seen {timeAgo(session.lastEventAt)}</p>}
    <Button type="button" variant="outline" size="sm" disabled>Resume</Button>
    <p>{`Resume works once ${own ? "your" : `${ownerName}'s`} Mac is back.`}{!own && canPickUp(session) ? " Pick up continues it on your Mac now." : ""}</p>
  </div>;
  // A terminal session with no terminal (an older Mac app): what a teammate can do is message the owner.
  const notice = sessionScreenNotice(session, own, ownerName);
  const reason = session.terminalReason === "plain" || session.terminalReason === "no-daemon" ? session.terminalReason : null;
  // No terminal here yet: what gets its screen shared (spec 2026-10-07 §4 f, g). One line over the conversation.
  const hint = reason && <p role="status" className="session-live-hint text-ui text-muted-foreground">{reason === "plain"
    ? own ? <>Press ← in its terminal to share this one live, or turn on Share every session live in the Lightbulb Mac app to make every new session live.</> : `Ask ${ownerName} to press ← in their terminal.`
    : own ? <>Start it again with <code>lightbulb codex</code> to share its screen.</> : <>Ask {ownerName} to start it again with <code>lightbulb codex</code>.</>}</p>;

  return <>
    <div className="session-status">
      <p className="session-status-line">
        <span className={`native-session-dot native-session-dot-${session.status}`} aria-hidden="true" />
        <span>{paused && session.status !== "ended" ? "Paused" : sessionStatus[session.status]}</span>
        <span>{own ? "You" : ownerName}</span>
        {where && <span className="session-fact-value room-wrap">{where}</span>}
        {session.pickedUpFrom && <span>picked up from {personName(session.pickedUpFrom.humanUserId)}</span>}
        {files > 0 && <button type="button" className="session-status-link" onClick={() => show("changes")}>
          {files} {files === 1 ? "file" : "files"} changed<span className="local-session-add">+{added}</span><span className="local-session-del">−{removed}</span></button>}
        {agents && <span>{agents}</span>}
        {pr && session.pr && <a className="session-status-link" href={session.pr.url} target="_blank" rel="noopener noreferrer">{pr}</a>}
        {history.error && <span role="status">Could not load session details. <button type="button" className="session-status-link" onClick={() => void history.retry()}>Retry</button></span>}
        {plan && plan.length > 0 && <button type="button" className="session-status-link" onClick={() => show("changes")}>plan {done} of {plan.length}</button>}
      </p>
      <div className="session-status-actions">
        {!own && canPickUp(session) && <SessionPickup session={session} ownerName={ownerName} own={false} client={client} />}
        {!own && !canPickUp(session) && session.provider === "claude" && session.pickup?.allowed === true && (session.pickup.state === "unavailable"
          ? <span role="status" className="text-ui text-muted-foreground">{pickupStatus(session.pickup, timeAgo)}</span>
          : <Button type="button" variant="outline" size="sm" disabled>Preparing…</Button>)}
        {!own && !canPickUp(session) && session.provider === "claude" && !session.pickup?.allowed && askPickup.supported &&
          <Button type="button" variant="outline" size="sm" disabled={askPickup.saving || (askPickup.state === "pending" && !askPickup.again)} onClick={() => void askPickup.ask()}>
            {askPickup.again ? "No answer yet. Ask again" : askPickup.state === "pending" ? "Requested" : askPickup.state === "declined" ? `${ownerName} declined. Ask again` : "Ask to pick up"}</Button>}
        {/* Gone once it ended: "Ending" next to "Ended" stayed for good (prod 2026-10-27). */}
        {command.attempt?.request.kind === "end" && command.chip && session.status !== "ended" && <span role="status" className="text-ui"><ReceiptChip chip={command.chip} /></span>}
        {/* The app's own terminal on this Mac has End in its bar; this is the same control everywhere else. */}
        {command.canEnd && local !== "local" && <Button type="button" variant="outline" size="sm" disabled={command.locked}
          onClick={() => { if (endArmed) command.end(); setEndArmed(!endArmed); }}>{endArmed ? `Click again to end “${sessionTitle(session)}”` : "End session"}</Button>}
        <Button type="button" variant="ghost" size="sm" onClick={() => show(tab)}><Info aria-hidden="true" />Details</Button>
      </div>
    </div>
    {own && !!session.pendingJoins?.length && <JoinRequests waiting={session.pendingJoins} ids={session.pendingJoinIds}
      line={name => `${name} wants to join this session.`} decide={(who, d, id) => client.decideLocalSessionJoin!(session.sessionId, who, d, id)}
      enabled={!!client.decideLocalSessionJoin} personName={personName} />}
    {own && !!session.pendingPickups?.length && <JoinRequests waiting={session.pendingPickups} ids={session.pendingPickupIds}
      line={pickupRequestLine} decide={(who, d, id) => client.decideLocalSessionPickup!(session.sessionId, who, d, id)}
      enabled={!!client.decideLocalSessionPickup} personName={personName} />}
    {(session.shared === false || (running && session.controlUnavailableReason) || session.workspace?.changed) && <div className="session-notices">
      {paused && session.status === "ended" && <p role="status">Only you see this session. Sharing again shares the whole session.</p>}
      {running && session.controlUnavailableReason && <p role="status" className="text-warn">{unavailableReason[session.controlUnavailableReason]}</p>}
      {session.workspace?.changed && <p role="status" className="text-warn">Files changed outside this session. It will re-read them before your next follow-up.</p>}
    </div>}
    {/* A prompt the Mac holds for a remote answer gets the approval card; any other waits in the terminal. Not over a
        live terminal on screen: its own prompt is the one surface there (owner ruling 2026-10-27). */}
    {!terminalOnScreen && <SessionApprovalCard key={session.pendingApproval?.approvalId} session={session} client={client} own={own} ownerName={ownerName} />}
    {<SessionQuestionCard key={`${session.generation}:${session.pendingQuestion?.promptId}`} session={session} client={client} own={own} ownerName={ownerName} />}
    {waitingOn !== null && !session.pendingApproval && !session.pendingQuestion && <div role="status" className="local-session-waiting">
      <p className="font-semibold">{question && canAnswer ? "Waiting for your answer" : own ? "Waiting on you in your terminal" : `Waiting on ${ownerName} in their terminal`}</p>
      {question && canAnswer ? <QuestionAnswer key={main.at(-1)?.seq} questions={question} keys={keys} /> : <>
        {waitingOn && <p className="local-session-waiting-reason">{waitingOn}</p>}
        {canAnswer ? <p className="text-ui">Answer it in the live terminal below.</p>
          : own ? <p className="text-ui">Answer it in your terminal; this prompt can&apos;t be answered from here.</p> : messageOwner}
      </>}
    </div>}
    {local !== "none"
      ? <>{audience && <div className="session-terminal-size local-session-audience"><span className="session-terminal-hint">{audience}</span></div>}
        <div ref={slot} className="local-session-terminal" role="region" aria-label="Session terminal" /></>
      : <>
      {convoEmpty ? endedPanel : convo ? <>{session.status === "ended" && endedLine}{session.status === "offline" && offlinePanel}<p className="session-ended-line" role="note">{notice.title} {notice.body}</p>
        {own && running && !CLI_ONLY.has(session.provider) && <OpenInTerminal sessionId={session.sessionId} onEvent={onEvent} />}</>
        : session.status === "ended" ? (endedScreen && !hit || keptWatching || savedConversation ? endedLine : endedPanel) : session.status === "offline" || paused ? offlinePanel : null}
      {savedConversation && session.controlUnavailableReason === "history-gap" && <p role="note" className="text-ui text-muted-foreground">Some earlier messages haven’t synced yet.</p>}
      {hit && running && !paused && hasTerminal && canWatch && <div className="local-session-actions">
        <Button type="button" variant="outline" size="sm" onClick={() => setLeftHit(focusSeq ?? null)}>Back to the live terminal</Button></div>}
      {conversation && !showBrowser && <><div className="local-session-scroll">
        <div ref={scroller} className="room-context-body local-session-history" role="region" aria-label="Native session history" tabIndex={0} onScroll={onScroll}>
          <div ref={content}>
            {history.loading && <div className="native-session-shimmer" role="status" aria-label="Loading session history" />}
            {feedback}
            {!history.loading && !history.error && history.events.length === 0 && !pendingText && <p className="native-session-meta">No shared output yet.</p>}
          </div>
        </div>
      </div></>}
      {browserTab && <div className="session-view-tabs" role="tablist" aria-label="Session view">
        <button type="button" role="tab" aria-selected={!showBrowser} onClick={() => setBrowserFor(null)}>{watchRemote ? "Terminal" : "Conversation"}</button>
        {browserTab && <button type="button" role="tab" aria-selected={showBrowser} onClick={() => setBrowserFor(session.sessionId)}>Browser</button>}
      </div>}
      {showBrowser && <SessionBrowserTab sessionId={session.sessionId} frames={frames} canType={control && running && session.shared !== false && !(session.inputPaused && !own)} />}
      {watchRemote && <div className="session-terminal-slot" hidden={showBrowser} onScrollCapture={endedScreen ? openAtTop : undefined}><SessionTerminalView session={session} onEvent={onEvent!} watch={client.watchLocalSession!} frames={frames} onLive={setScreenLive} onCanType={setTermCanType} kept={endedScreen} keys={keys}
        admitted={running && control && session.shared !== false && !(session.inputPaused && !own)} resizable={session.terminalResize === true}
        input={client.sendLocalSessionInput ? (bytes, epoch, size) => client.sendLocalSessionInput!(session.sessionId, toBase64(bytes), epoch, size) : undefined} /></div>}
      {terminalLive && control && !own && session.inputPaused && <p className="text-ui text-muted-foreground">Paused while {ownerName} answers a request to join.</p>}
      {/* Under the live terminal only what it cannot show: a sent follow-up not echoed yet, and the queue. */}
      {liveTerminal && !showBrowser && <div className="local-session-feedback">{feedback}</div>}
      {/* One child in a reversed column: the browser keeps the newest row in view as the conversation grows. */}
      {showBrowser ? null : convo && !convoEmpty ? <div className="local-session-feedback local-session-conversation" role="log" aria-label="Session conversation"><div>{feedback}</div></div>
        : !liveTerminal && !conversation && (session.status !== "ended" || !!pendingText || queuedOutbox.length > 0) && <div className="local-session-feedback">{feedback}</div>}
    </>}
    {session.status === "ended" ? <SessionResume flow={resume} own={own} ownerName={ownerName} /> : showBrowser ? null : terminalLive && control && (local === "local" || termCanType || (!own && session.inputPaused)) ? null : waitingOn !== null ? null : control ? <form className="room-composer local-session-composer native-session-composer" onSubmit={event => { event.preventDefault(); atBottomRef.current = true; command.send(text); }}>
      <label htmlFor={id} className="sr-only">Send to {target}</label>
      <div className="native-session-prompt-row"><span className="native-session-glyph" aria-hidden="true">{glyph}</span>
        <textarea id={id} className="room-input" rows={2} value={text} disabled={!command.canSend || command.locked} onChange={event => setText(event.target.value)}
          onKeyDown={event => { if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing && event.keyCode !== 229) { event.preventDefault(); event.currentTarget.form?.requestSubmit(); } }}
          placeholder="Send a follow-up…" />
        <Button type="submit" size="icon-sm" className="room-send" aria-label="Send follow-up" disabled={!command.canSend || command.locked || !text.trim()}><Send aria-hidden="true" /></Button></div>
      {footer && <p className="native-session-footer" title="Set in the terminal. It cannot be changed from here.">{footer}</p>}
      <ResumeDelivery flow={resume} />
      {(command.canSteer || command.canInterrupt) && <div className="local-session-actions">
        {command.canSteer && <Button type="button" variant="outline" size="sm" disabled={command.locked || !text.trim()} onClick={() => command.steer(text)}>Steer now</Button>}
        {command.canInterrupt && <Button type="button" variant="outline" size="sm" disabled={command.locked} onClick={command.interrupt}>Stop turn</Button>}
        <span className="text-caption text-muted-foreground">Send waits for the next turn.</span>
      </div>}
      {session.status === "offline" && !blocked && <p className="text-ui text-muted-foreground">Mac offline. Follow-ups wait up to 24 hours for this session to reconnect.</p>}
      {command.canCancel && <Button type="button" variant="outline" size="sm" disabled={command.checking} onClick={() => void command.cancel()}>Cancel queued follow-up</Button>}
      {blocked && !(running && session.controlUnavailableReason) && <p className="text-ui text-warn">{blocked}</p>}
      {command.attempt?.state === "rejected" && !command.attempt.receipt && command.attempt.error && <p role="alert" className="text-ui text-warn">{command.attempt.error}</p>}
      {/* Once the harness has echoed the prompt the pending row is gone; a warning still needs a home. */}
      {!pendingText && command.chip && (command.chip.tone === "warn" || command.chip.tone === "error" || command.attempt?.request.kind === "interrupt") && command.attempt?.receipt && <p className="text-ui"><ReceiptChip chip={command.chip} /></p>}
      {(command.canCheck || command.attempt?.state === "uncertain") && <div className="local-session-actions">
        {command.canCheck && <Button type="button" variant="outline" size="sm" disabled={command.checking} onClick={() => void command.checkDelivery()}>Check delivery</Button>}
        {command.attempt?.state === "uncertain" && <Button type="button" variant="outline" size="sm" disabled={!command.canSend || command.checking} onClick={() => command.retry()}>Retry same request</Button>}
      </div>}
    </form> : <div className="room-composer local-session-readonly">
      <div>
        <p className="text-ui text-muted-foreground">{terminalLive && "Watching · "}{watchOnlyLine(ownerName)}</p>
        <div className="local-session-actions">
          {waitingOn === null && !(notStarted && convo) && messageOwner}
        </div>
      </div>
      {footer && <p className="native-session-footer">{footer}</p>}
    </div>}
    <SessionDetails open={details !== null} onOpenChange={open => { if (!open) setDetails(null); }} onClosed={() => opener.current?.focus()} tab={tab} onTab={setTab}
      session={session} ownerName={ownerName} own={own} client={client} events={main} terminalHint={hint} onMessageOwner={onMessageOwner} nameOf={personName} />
  </>;
}

/** The owner's own app session (Claude desktop, the Codex app, an IDE), forked into a live held terminal on their
 *  Mac. The server and the Mac refuse anyone else; the app's own session keeps running. */
function OpenInTerminal({ sessionId, onEvent }: { sessionId: string; onEvent?: SessionSource["onEvent"] }) {
  const api = useApi();
  const [open, setOpen] = useState<{ startId: string; phase: "opening" | "opened" | "failed"; error?: string } | null>(null);
  useEffect(() => {
    if (!onEvent || open?.phase !== "opening") return;
    return onEvent((event) => {
      if (event.type !== "runtime.operation.changed" || event.payload.operationId !== open.startId) return;
      setOpen(o => o && (event.payload.state === "succeeded" ? { ...o, phase: "opened" }
        : event.payload.state === "failed" ? { ...o, phase: "failed", error: event.payload.error?.message } : o));
    });
  }, [onEvent, open]);
  const start = async () => {
    const startId = crypto.randomUUID();
    setOpen({ startId, phase: "opening" });
    try { await api.post(`/desktop-sessions/open/${sessionId}`, { startId }); }
    catch { setOpen({ startId, phase: "failed", error: "Your Mac did not answer. Check that Lightbulb is open on it." }); }
  };
  return <div className="local-session-actions">
    <Button type="button" variant="outline" size="sm" disabled={open?.phase === "opening"} onClick={() => void start()}>
      {open?.phase === "opening" ? "Opening…" : "Open in a terminal"}</Button>
    <p role="status" className="text-ui text-muted-foreground">{open?.phase === "opened" ? "Opened. It is in your sessions as a new terminal session."
      : open?.phase === "failed" ? open.error ?? "It did not open." : "Opens this conversation in a live terminal on your Mac. The app keeps its copy."}</p>
  </div>;
}

/** The owner answers "Ask to join" / "Ask to pick up" in the session itself, where a request's notification lands. */
function JoinRequests({ waiting, ids, line, decide: send, enabled, personName }: {
  waiting: string[]; ids?: Record<string, string>; line: (name: string) => string; enabled: boolean;
  decide: (who: string, decision: "allow" | "decline", id?: string) => Promise<unknown>; personName: (who: string) => string;
}) {
  // One entry per decision in flight, so finishing one never re-enables another's buttons.
  const [busy, setBusy] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const decide = async (who: string, decision: "allow" | "decline") => {
    if (!enabled) return;
    setBusy(list => [...list, who]); setError(null);
    try { await send(who, decision, ids?.[who]); }
    catch { setError("Could not answer that request. Try again."); }
    finally { setBusy(list => list.filter(x => x !== who)); }
  };
  return <div className="session-notices">
    {waiting.map(who => <div key={who} role="status" className="local-session-join-request">
      <p>{line(personName(who))}</p>
      <div className="local-session-actions">
        <Button type="button" size="sm" disabled={busy.includes(who)} onClick={() => void decide(who, "allow")}>Allow</Button>
        <Button type="button" variant="outline" size="sm" disabled={busy.includes(who)} onClick={() => void decide(who, "decline")}>Decline</Button>
      </div>
    </div>)}
    {error && <p role="alert" className="text-warn">{error}</p>}
  </div>;
}

/**
 * A Claude question (AskUserQuestion) answered from here: the picks are typed into the session's live
 * terminal, one key per write (`questionKeys`; observed on Claude Code 2.1.310: a single-select question
 * takes its option's digit and moves on, so one question is answered by one click; a multi-select one
 * toggles by digit and moves on with →; two or more questions, or any multi-select, end on a review
 * screen that takes Enter). "Type something" stays in the terminal.
 */
function QuestionAnswer({ questions, keys }: { questions: SessionQuestion[]; keys: React.RefObject<((data: string) => Promise<unknown>) | null> }) {
  const [picks, setPicks] = useState<number[][]>(() => questions.map(() => []));
  // "sent" only once the relay took every key. A key the server refused before sending (`KEY_NOT_SENT`) was
  // never typed: the rest is sent again from it ("failed"). Any other failure leaves unknown whether it
  // landed, and a key typed twice answers the next question or the composer: no retry ("stuck").
  const [state, setState] = useState<"idle" | "sending" | "sent" | "failed" | "stuck">("idle");
  const progress = useRef<{ keys: string[]; taken: number } | null>(null);
  const [taken, setTaken] = useState(0);
  const sent = state === "sending" || state === "sent" || state === "stuck";
  const send = async (answer: number[][]) => {
    // A retry after a partial send resumes the same keys; with nothing taken yet it starts over with this answer.
    const run = progress.current && progress.current.taken > 0 ? progress.current : { keys: questionKeys(questions, answer), taken: 0 };
    progress.current = run;
    setState("sending");
    for (let i = run.taken; i < run.keys.length; i++) {
      if (i > 0) await new Promise(done => setTimeout(done, KEY_GAP_MS));
      try {
        if (!keys.current) throw new ApiError(409, { code: "LOCAL_SESSION_UNSUPPORTED" }, "no terminal");
        await keys.current(run.keys[i]);
      } catch (e) {
        setState(keyNotSent(e) ? "failed" : "stuck");
        return;
      }
      run.taken = i + 1; setTaken(run.taken);
    }
    setState("sent");
  };
  // One single-select question: a click is the answer.
  const oneClick = questions.length === 1 && !questions[0].multiSelect;
  const pick = (q: number, o: number) => {
    if (oneClick) { void send([[o]]); return; }
    setPicks(all => all.map((p, i) => i !== q ? p : questions[q].multiSelect ? (p.includes(o) ? p.filter(x => x !== o) : [...p, o].sort()) : [o]));
  };
  const ready = picks.every((p, i) => p.length > 0 || questions[i].multiSelect);
  return <div className="local-session-question">
    {questions.map((q, qi) => <fieldset key={qi} disabled={sent || taken > 0}>
      <legend className="local-session-waiting-reason">{q.question}</legend>
      <div className="local-session-actions">
        {q.options.map((label, oi) => <Button key={oi} type="button" size="sm" variant={picks[qi].includes(oi) ? "default" : "outline"}
          aria-pressed={oneClick ? undefined : picks[qi].includes(oi)} onClick={() => pick(qi, oi)}>{label}</Button>)}
      </div>
    </fieldset>)}
    {(!oneClick || taken > 0) && <Button type="button" size="sm" disabled={sent || !ready} onClick={() => void send(picks)}>{state === "failed" ? "Try again" : "Send answer"}</Button>}
    {state === "sent" && <p role="status" className="text-ui text-muted-foreground">Sent to the terminal.</p>}
    {state === "failed" && <p role="alert" className="text-ui text-warn">Couldn&apos;t send, try again.</p>}
    {state === "stuck" && <p role="alert" className="text-ui text-warn">Couldn&apos;t send. Finish answering in the terminal.</p>}
  </div>;
}
