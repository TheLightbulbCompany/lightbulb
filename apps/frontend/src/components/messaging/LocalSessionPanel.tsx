"use client";

import { useCallback, useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useAuth } from "@clerk/nextjs";
import { capture } from "@/lib/analytics";
import { Info, MessageSquare, Send } from "lucide-react";
import {
  canControlSession, useSessionResume, mainEvents, pendingApproval, receiptChip, sendBlockedReason, sessionChanges, sessionFooter, sessionLabel, sessionPlan, sessionStatus, unavailableReason,
  pickupRequestLine, pickupStatus, useHistorySource, useLocalSessionHistory, usePendingPrompt, useSessionAudit, useSessionCommand, useSessionJoinRequest, useSessionOutbox, withAuditNotices,
  type LocalSession, type SessionControlClient, type SessionSource,
} from "@lightbulb/shared";
import Link from "next/link";
import { Button } from "@/components/ui/button";
import { isDesktopApp } from "@/lib/desktopNotify";
import { NativeSessionTranscript, ReceiptChip, SubagentPanel, revealSubagent, type Person } from "./NativeSessionTranscript";
import { SessionApprovalCard } from "./SessionControls";
import { SessionTerminalView } from "./SessionTerminalView";
import { useLocalTerminal } from "./localTerminal";
import { toBase64 } from "./sessionInput";
import { SessionDetails, type SessionDetailsTab } from "./SessionDetails";
import { canPickUp, SessionPickup } from "./SessionPickup";
import { timeAgo } from "@/components/teams/shared/lib/timeAgo";
import type { PickupClient } from "./useSessionPickup";
import { ResumeDelivery, SessionResume } from "./SessionResume";

/** Distance from the bottom, in px, still counted as "at the bottom" (mobile's value). */
const BOTTOM_SLACK = 80;
/** Within this many pixels of the top, older output is fetched. */
const SCROLLBACK_SLACK = 240;

type Decide = (sessionId: string, userId: string, decision: "allow" | "decline", requestId?: string) => Promise<unknown>;
type JoinDecider = { decideLocalSessionJoin?: Decide; decideLocalSessionPickup?: Decide };
type Client = SessionSource["client"] & SessionControlClient & PickupClient & Parameters<typeof SessionDetails>[0]["client"] & JoinDecider & { watchLocalSession?: (sessionId: string) => Promise<unknown>; sendLocalSessionInput?: (sessionId: string, data: string, epoch: number) => Promise<unknown> };

/**
 * A shared session: its status on one line, the conversation as its terminal
 * draws it, and the prompt. What the session is running, what it changed and
 * who may do what with it live behind Details, so the transcript has the room.
 */
export function LocalSessionPanel({ session, ownerName, own, client, revision, onAccessLost, participants = [], onMessageOwner, nameOf, onEvent }: {
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
  const busy = session.status === "busy";
  // Backstop poll while the session is producing output: the relay throttles
  // transcript hints (1/s per session, no trailing edge), so hints alone can
  // leave an open transcript behind during sustained output. Idle sessions
  // stay on hints plus the sessions safety poll. Quiet: no spinner, no focus steal.
  const source = useHistorySource(client, session, revision);
  const history = useLocalSessionHistory(client, source.sessionId, source.latestEventSeq, revision, onAccessLost, { pollMs: busy ? 5000 : 0 });
  const [text, setText] = useState("");
  const [details, setDetails] = useState<SessionDetailsTab | null>(null);
  const [tab, setTab] = useState<SessionDetailsTab>("about");
  const slot = useRef<HTMLDivElement>(null);
  // Running on this Mac: the real terminal replaces the session view (rulings 2026-10-03 §2).
  // A live terminal only where the Mac has one (rulings 2026-10-04): held, or a Claude background
  // session through `claude attach`. A plain interactive Claude and any plain Codex: the transcript.
  const hasTerminal = !!session.terminal;
  const running = session.status !== "ended" && session.status !== "offline";
  // An offline session still probes this Mac's holder: "offline" is the relay's view (sharing off, a reconnect), not the Mac's.
  const local = useLocalTerminal(session.status === "ended" || (running && !hasTerminal) ? null : { sessionId: session.sessionId }, slot, details === null);
  const watchRemote = local === "none" && hasTerminal && running && !!onEvent && !!client.watchLocalSession;
  const [screenLive, setScreenLive] = useState(false);
  // False once the terminal is read-only for this viewer (the Mac refused input): the message box is the way to send then.
  const [termCanType, setTermCanType] = useState(true);
  const liveTerminal = watchRemote && screenLive;
  // Not running: this is the structured transcript, not a terminal. The Mac's own name is not in the public record, so the owner's name stands for it.
  const transcriptLabel = local !== "none" ? null
    : session.status === "ended" ? "Transcript · Ended"
    : session.status === "offline" ? `Transcript · ${own ? "your" : `${ownerName}'s`} Mac is offline`
    : hasTerminal ? null : `Transcript · ${sessionStatus[session.status]}`;
  // ← in a plain Claude session moves it to the background, where Lightbulb can attach to it.
  const pressLeft = own && session.provider === "claude" && !hasTerminal && running;
  const id = useId();
  const people = useMemo(() => [{ kind: "human" as const, id: session.humanUserId, displayName: ownerName }, ...participants], [session.humanUserId, ownerName, participants]);
  const agentName = useCallback((agentId: string) => people.find(p => p.id === agentId)?.displayName ?? "Agent", [people]);
  const personName = useCallback((who: string) => nameOf?.(who) ?? people.find(p => p.id === who)?.displayName ?? "A teammate", [nameOf, people]);
  const command = useSessionCommand(client, session, { uuid: () => crypto.randomUUID(), onAccessLost, onSent: () => setText(""), agentName, allowInterrupt: own || !!session.teamControl, allowOffline: true });
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
  const waitingOn = session.status === "waiting-approval" ? pendingApproval(main) ?? "" : null;
  const target = sessionLabel(session);
  const { userId, orgId } = useAuth();
  // One row per session a person opens: whether anyone looks at a teammate's session is the
  // first question the sharing feature has to answer. No title, no path, no text.
  useEffect(() => {
    capture("session_opened", { own, provider: session.provider, workspace_id: orgId, session_id: session.sessionId, origin: "web" });
  }, [session.sessionId, own, session.provider, orgId]);
  const control = canControlSession(session, own, userId);
  // A live terminal is the only input: no message box under it. A viewer who cannot type keeps the Ask to join line.
  const terminalLive = local === "local" || liveTerminal;
  // "<Name> is typing": the owner sees who is typing into the live terminal for ~6 s after each event.
  const [typers, setTypers] = useState<Record<string, number>>({});
  useEffect(() => {
    if (!onEvent) return;
    const timers = new Set<ReturnType<typeof setTimeout>>();
    const off = onEvent((event) => {
      if (event.type !== "workspace.session.typing" || event.payload.sessionId !== session.sessionId || event.payload.userId === userId) return;
      const who = event.payload.userId;
      const at = Date.now();
      setTypers((t) => ({ ...t, [who]: at }));
      const timer = setTimeout(() => { timers.delete(timer); setTypers((t) => { if (t[who] !== at) return t; const { [who]: _gone, ...rest } = t; return rest; }); }, 6000);
      timers.add(timer);
    });
    return () => { off(); timers.forEach(clearTimeout); };
  }, [onEvent, session.sessionId, userId]);
  const typing = Object.keys(typers).map(personName);
  const resume = useSessionResume(client, session, { own, userId, ownerName, nameOf: personName, uuid: () => crypto.randomUUID() });
  const join = useSessionJoinRequest(client, session);
  const askPickup = useSessionJoinRequest(client, session, "pickup");
  const audit = useSessionAudit(client, session.sessionId,
    `${revision}:${session.latestEventSeq}:${session.teamControl}:${session.allowedSenders?.join()}:${session.joinRequest}:${session.pickup?.allowed}:${session.status}:${session.pendingApproval?.approvalId}`);
  const events = withAuditNotices(history.events, audit, personName);
  const glyph = session.provider === "claude" ? "❯" : "›";
  const plan = useMemo(() => sessionPlan(main), [main]);
  const changes = useMemo(() => sessionChanges(main), [main]);
  const footer = sessionFooter(session);
  // The status line has room for the repository's own name and its branch; Details has the rest.
  const repo = session.pickup?.hasCode ? session.pickup.repo?.split("/").pop() : undefined;
  const where = repo ? [repo, session.pickup?.branch].filter(Boolean).join(" on ") : session.cwdName ? `~/${session.cwdName}` : "";
  // Details opens from the button and from the status line's links; focus returns to whichever it was.
  const opener = useRef<HTMLElement | null>(null);
  const show = (next: SessionDetailsTab) => {
    opener.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    setTab(next); setDetails(next);
  };

  // Like a terminal: open at the bottom, stick to it while the reader is there,
  // stop the moment they scroll up, and fetch older output on its own as they
  // near the top. Growth is watched on the content box, so a tool row filling in
  // (no new seq) still re-pins.
  const scroller = useRef<HTMLDivElement>(null);
  const content = useRef<HTMLDivElement>(null);
  const atBottomRef = useRef(true);
  const toBottom = useCallback(() => { const node = scroller.current; if (node) node.scrollTop = node.scrollHeight; }, []);
  // Height before an older page lands, so the reader stays on the line they were reading.
  const anchor = useRef<{ height: number; top: number } | null>(null);
  useLayoutEffect(() => {
    const node = scroller.current;
    if (!node || !anchor.current) return;
    node.scrollTop = node.scrollHeight - anchor.current.height + anchor.current.top;
    anchor.current = null;
  }, [history.events]);
  // A window shorter than the pane cannot be scrolled up, so fill it the way a terminal's scrollback would.
  useEffect(() => {
    const node = scroller.current;
    if (node && !history.loading && !history.error && history.hasOlder && node.scrollHeight <= node.clientHeight) void history.load("older");
  }, [history]);
  useEffect(() => {
    const node = content.current;
    if (!node || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => { if (atBottomRef.current) toBottom(); });
    observer.observe(node);
    return () => observer.disconnect();
  }, [toBottom, liveTerminal]);
  const onScroll = () => {
    const node = scroller.current;
    if (!node) return;
    atBottomRef.current = node.scrollHeight - node.scrollTop - node.clientHeight <= BOTTOM_SLACK;
    if (history.loading) return;
    if (node.scrollTop < SCROLLBACK_SLACK && history.hasOlder) {
      anchor.current = { height: node.scrollHeight, top: node.scrollTop };
      void history.load("older");
    } else if (atBottomRef.current && history.hasNewer) void history.load("newer");
  };
  const messageOwner = !own && onMessageOwner
    ? <Button type="button" variant="outline" size="sm" onClick={onMessageOwner}><MessageSquare aria-hidden="true" />Message {ownerName}</Button> : null;
  const added = changes.reduce((n, file) => n + file.added, 0);
  const removed = changes.reduce((n, file) => n + file.removed, 0);
  const done = plan?.filter(step => step.status === "completed").length ?? 0;

  // The transcript, or (under a live terminal) just its pending and queued rows: sent follow-ups stay visible.
  const transcript = <>
          <NativeSessionTranscript provider={session.provider} events={liveTerminal ? [] : events} busy={busy && !history.hasNewer && !liveTerminal} ownerName={ownerName} participants={people}
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

  return <>
    <div className="session-status">
      <p className="session-status-line">
        <span className={`native-session-dot native-session-dot-${session.status}`} aria-hidden="true" />
        <span>{transcriptLabel ?? sessionStatus[session.status]}</span>
        {!(transcriptLabel && session.status === "offline") && <span>{own ? "You" : ownerName}</span>}
        {where && <span className="session-fact-value room-wrap">{where}</span>}
        {session.pickedUpFrom && <span>picked up from {personName(session.pickedUpFrom.humanUserId)}</span>}
        {changes.length > 0 && <button type="button" className="session-status-link" onClick={() => show("changes")}>
          {changes.length} {changes.length === 1 ? "file" : "files"} changed<span className="local-session-add">+{added}</span><span className="local-session-del">−{removed}</span></button>}
        {plan && plan.length > 0 && <button type="button" className="session-status-link" onClick={() => show("changes")}>plan {done} of {plan.length}</button>}
      </p>
      <div className="session-status-actions">
        {!own && canPickUp(session) && <SessionPickup session={session} ownerName={ownerName} own={false} client={client} />}
        {!own && !canPickUp(session) && session.provider === "claude" && session.pickup?.allowed === true && (session.pickup.state === "unavailable"
          ? <span role="status" className="text-sm text-muted-foreground">{pickupStatus(session.pickup, timeAgo)}</span>
          : <Button type="button" variant="outline" size="sm" disabled>Preparing…</Button>)}
        {!own && !canPickUp(session) && session.provider === "claude" && !session.pickup?.allowed && askPickup.supported &&
          <Button type="button" variant="outline" size="sm" disabled={askPickup.saving || askPickup.state === "pending"} onClick={() => void askPickup.ask()}>
            {askPickup.state === "pending" ? "Requested" : askPickup.state === "declined" ? `${ownerName} declined. Ask again` : "Ask to pick up"}</Button>}
        <Button type="button" variant="ghost" size="sm" onClick={() => show(tab)}><Info aria-hidden="true" />Details</Button>
      </div>
    </div>
    {own && !!session.pendingJoins?.length && <JoinRequests waiting={session.pendingJoins} ids={session.pendingJoinIds}
      line={name => `${name} wants to join this session.`} decide={(who, d, id) => client.decideLocalSessionJoin!(session.sessionId, who, d, id)}
      enabled={!!client.decideLocalSessionJoin} personName={personName} />}
    {own && !!session.pendingPickups?.length && <JoinRequests waiting={session.pendingPickups} ids={session.pendingPickupIds}
      line={pickupRequestLine} decide={(who, d, id) => client.decideLocalSessionPickup!(session.sessionId, who, d, id)}
      enabled={!!client.decideLocalSessionPickup} personName={personName} />}
    {(pressLeft || session.shared === false || session.controlUnavailableReason || session.workspace?.changed) && <div className="session-notices">
      {pressLeft && <p role="status">Press ← in its terminal to share this one live, or turn on Share every session live in {isDesktopApp()
        ? <Link className="underline underline-offset-4" href="/settings?panel=desktop#share-every-session-live">Settings</Link>
        : "Settings"} to make every new session live.</p>}
      {session.shared === false && <p role="status">Sharing is paused. Only you can see this session, and nothing it does now is shared.</p>}
      {session.controlUnavailableReason && <p role="status" className="text-warn">{unavailableReason[session.controlUnavailableReason]}</p>}
      {session.workspace?.changed && <p role="status" className="text-warn">Files changed outside this session. It will re-read them before your next follow-up.</p>}
    </div>}
    {/* A prompt the Mac holds for a remote answer gets the approval card; any other waits in the terminal. */}
    <SessionApprovalCard session={session} client={client} own={own} ownerName={ownerName} />
    {waitingOn !== null && !session.pendingApproval && <div role="status" className="local-session-waiting">
      <p className="font-semibold">{own ? "Waiting on you in your terminal" : `Waiting on ${ownerName} in their terminal`}</p>
      {waitingOn && <p className="local-session-waiting-reason">{waitingOn}</p>}
      {liveTerminal && termCanType && control && session.shared !== false && client.sendLocalSessionInput
        ? <p className="text-sm">Answer it in the live terminal below.</p>
        : own ? <p className="text-sm">Answer it in your terminal; this prompt can&apos;t be answered from here.</p> : messageOwner}
    </div>}
    {typing.length > 0 && <p role="status" className="text-sm text-muted-foreground">{typing.join(", ")} {typing.length === 1 ? "is" : "are"} typing</p>}
    {local !== "none"
      ? <div ref={slot} className="local-session-terminal" role="region" aria-label="Session terminal" />
      : <>
      {watchRemote && <SessionTerminalView session={session} onEvent={onEvent!} watch={client.watchLocalSession!} onLive={setScreenLive} onCanType={setTermCanType}
        admitted={control && session.shared !== false && !(session.inputPaused && !own)}
        input={client.sendLocalSessionInput ? (bytes, epoch) => client.sendLocalSessionInput!(session.sessionId, toBase64(bytes), epoch) : undefined} />}
      {terminalLive && control && !own && session.inputPaused && <p className="text-sm text-muted-foreground">Paused while {ownerName} answers a request to join.</p>}
      {liveTerminal ? <div className="local-session-feedback">{transcript}</div> : <div className="local-session-scroll">
      <div ref={scroller} className="room-context-body local-session-history" role="region" aria-label="Native session history" tabIndex={0} onScroll={onScroll}>
        <div ref={content}>
          {history.loading && <div className="native-session-shimmer" role="status" aria-label="Loading session history" />}
          {transcript}
          {history.error && <div role="alert"><p>{history.error}</p><Button variant="outline" onClick={() => void history.retry()}>Retry history</Button></div>}
          {!history.loading && !history.error && history.events.length === 0 && !pendingText && <p>{session.provider === "claude" && session.status === "ended" && !session.controlUnavailableReason ? "This session ended before anything was sent." : "No shared output yet."}</p>}

        </div>
      </div>
    </div>}
    </>}
    <SubagentPanel provider={session.provider} events={history.events} status={session.status} onPick={id => revealSubagent(content.current, id)} />
    {session.status === "ended" ? <SessionResume flow={resume} own={own} ownerName={ownerName} /> : terminalLive && control && (local === "local" || termCanType || (!own && session.inputPaused)) ? null : control ? <form className="room-composer local-session-composer native-session-composer" onSubmit={event => { event.preventDefault(); atBottomRef.current = true; command.send(text); }}>
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
        <span className="text-xs text-muted-foreground">Send waits for the next turn.</span>
      </div>}
      {session.status === "offline" && !blocked && <p className="text-sm text-muted-foreground">Mac offline. Follow-ups wait up to 24 hours for this session to reconnect.</p>}
      {command.canCancel && <Button type="button" variant="outline" size="sm" disabled={command.checking} onClick={() => void command.cancel()}>Cancel queued follow-up</Button>}
      {blocked && <p className="text-sm text-warn">{blocked}</p>}
      {command.attempt?.state === "rejected" && !command.attempt.receipt && command.attempt.error && <p role="alert" className="text-sm text-warn">{command.attempt.error}</p>}
      {/* Once the harness has echoed the prompt the pending row is gone; a warning still needs a home. */}
      {!pendingText && command.chip && (command.chip.tone === "warn" || command.chip.tone === "error" || command.attempt?.request.kind === "interrupt") && command.attempt?.receipt && <p className="text-sm"><ReceiptChip chip={command.chip} /></p>}
      {(command.canCheck || command.attempt?.state === "uncertain") && <div className="local-session-actions">
        {command.canCheck && <Button type="button" variant="outline" size="sm" disabled={command.checking} onClick={() => void command.checkDelivery()}>Check delivery</Button>}
        {command.attempt?.state === "uncertain" && <Button type="button" variant="outline" size="sm" disabled={!command.canSend || command.checking} onClick={() => command.retry()}>Retry same request</Button>}
      </div>}
    </form> : <div className="room-composer local-session-readonly">
      <div>
        <p className="text-sm text-muted-foreground">{terminalLive && "Watching · "}{join.state === "declined" ? `${ownerName} declined your request.` : `Only ${ownerName} can send to this session.`}</p>
        <div className="local-session-actions">
          {join.supported && <Button type="button" variant="outline" size="sm" disabled={join.saving || join.state === "pending"}
            onClick={() => void join.ask()}>{join.state === "pending" ? "Requested" : "Ask to join"}</Button>}
          {waitingOn === null && messageOwner}
        </div>
        {join.error && <p role="alert" className="text-sm text-warn">{join.error}</p>}
      </div>
      {footer && <p className="native-session-footer">{footer}</p>}
    </div>}
    <SessionDetails open={details !== null} onOpenChange={open => { if (!open) setDetails(null); }} onClosed={() => opener.current?.focus()} tab={tab} onTab={setTab}
      session={session} ownerName={ownerName} own={own} client={client} events={main} onMessageOwner={onMessageOwner} nameOf={personName} />
  </>;
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
