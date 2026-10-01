"use client";

import { useCallback, useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useAuth } from "@clerk/nextjs";
import { capture } from "@/lib/analytics";
import { Info, MessageSquare, Send } from "lucide-react";
import {
  canControlSession, mainEvents, pendingApproval, receiptChip, sendBlockedReason, sessionChanges, sessionFooter, sessionLabel, sessionPlan, sessionStatus, unavailableReason,
  useLocalSessionHistory, usePendingPrompt, useSessionAudit, useSessionCommand, useSessionJoinRequest, useSessionOutbox, withAuditNotices,
  type LocalSession, type SessionControlClient, type SessionSource,
} from "@lightbulb/shared";
import { Button } from "@/components/ui/button";
import { NativeSessionTranscript, ReceiptChip, SubagentPanel, revealSubagent, type Person } from "./NativeSessionTranscript";
import { SessionApprovalCard } from "./SessionControls";
import { SessionDetails, type SessionDetailsTab } from "./SessionDetails";
import { canPickUp, SessionPickup } from "./SessionPickup";
import type { PickupClient } from "./useSessionPickup";

/** Distance from the bottom, in px, still counted as "at the bottom" (mobile's value). */
const BOTTOM_SLACK = 80;
/** Within this many pixels of the top, older output is fetched. */
const SCROLLBACK_SLACK = 240;

type Client = SessionSource["client"] & SessionControlClient & PickupClient & Parameters<typeof SessionDetails>[0]["client"] & JoinDecider;

/**
 * A shared session: its status on one line, the conversation as its terminal
 * draws it, and the prompt. What the session is running, what it changed and
 * who may do what with it live behind Details, so the transcript has the room.
 */
export function LocalSessionPanel({ session, ownerName, own, client, revision, onAccessLost, participants = [], onMessageOwner, nameOf }: {
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
  const history = useLocalSessionHistory(client, session.sessionId, session.latestEventSeq, revision, onAccessLost, { pollMs: busy ? 5000 : 0 });
  const [text, setText] = useState("");
  const [details, setDetails] = useState<SessionDetailsTab | null>(null);
  const [tab, setTab] = useState<SessionDetailsTab>("about");
  const id = useId();
  const people = useMemo(() => [{ kind: "human" as const, id: session.humanUserId, displayName: ownerName }, ...participants], [session.humanUserId, ownerName, participants]);
  const agentName = useCallback((agentId: string) => people.find(p => p.id === agentId)?.displayName ?? "Agent", [people]);
  const personName = useCallback((who: string) => nameOf?.(who) ?? people.find(p => p.id === who)?.displayName ?? "A teammate", [nameOf, people]);
  const command = useSessionCommand(client, session, { uuid: () => crypto.randomUUID(), onAccessLost, onSent: () => setText(""), agentName, allowInterrupt: own || !!session.teamControl, allowOffline: true });
  const outbox = useSessionOutbox(client, session.sessionId,
    `${session.status}:${session.status === "offline" ? revision : 0}:${command.attempt?.request.requestId}:${command.receipt?.state}`,
    session.status !== "offline" && session.status !== "ended");
  // A subagent's rows are tagged and read in its own transcript, never in the session's.
  const main = useMemo(() => mainEvents(history.events), [history.events]);
  const savedOutbox = outbox.commands.filter(receipt =>
    receipt.requestId !== command.attempt?.request.requestId &&
    (receipt.state !== "applied" || !main.some(event => event.role === "user" &&
      (event.originRequestId === receipt.requestId || (!!receipt.providerMessageId && event.itemId === receipt.providerMessageId)))));
  const pendingText = usePendingPrompt(command.attempt, main);
  const blocked = sendBlockedReason(session);
  const waitingOn = session.status === "waiting-approval" ? pendingApproval(main) ?? "" : null;
  const target = sessionLabel(session);
  const { userId, orgId } = useAuth();
  // One row per session a person opens: whether anyone looks at a teammate's session is the
  // first question the sharing feature has to answer. No title, no path, no text.
  useEffect(() => {
    capture("session_opened", { own, provider: session.provider, workspace_id: orgId });
  }, [session.sessionId, own, session.provider, orgId]);
  const control = canControlSession(session, own, userId);
  const join = useSessionJoinRequest(client, session);
  const audit = useSessionAudit(client, session.sessionId,
    `${revision}:${session.latestEventSeq}:${session.teamControl}:${session.allowedSenders?.join()}:${session.joinRequest}:${session.pickup?.allowed}:${session.status}`);
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
  }, [toBottom]);
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

  return <>
    <div className="session-status">
      <p className="session-status-line">
        <span className={`native-session-dot native-session-dot-${session.status}`} aria-hidden="true" />
        <span>{sessionStatus[session.status]}</span>
        <span>{own ? "You" : ownerName}</span>
        {where && <span className="session-fact-value room-wrap">{where}</span>}
        {session.pickedUpFrom && <span>picked up from {personName(session.pickedUpFrom.humanUserId)}</span>}
        {changes.length > 0 && <button type="button" className="session-status-link" onClick={() => show("changes")}>
          {changes.length} {changes.length === 1 ? "file" : "files"} changed<span className="local-session-add">+{added}</span><span className="local-session-del">−{removed}</span></button>}
        {plan && plan.length > 0 && <button type="button" className="session-status-link" onClick={() => show("changes")}>plan {done} of {plan.length}</button>}
      </p>
      <div className="session-status-actions">
        {!own && canPickUp(session) && <SessionPickup session={session} ownerName={ownerName} own={false} client={client} />}
        <Button type="button" variant="ghost" size="sm" onClick={() => show(tab)}><Info aria-hidden="true" />Details</Button>
      </div>
    </div>
    {own && !!session.pendingJoins?.length && <JoinRequests session={session} client={client} personName={personName} />}
    {(session.shared === false || session.controlUnavailableReason || session.workspace?.changed) && <div className="session-notices">
      {session.shared === false && <p role="status">Sharing is paused. Only you can see this session, and nothing it does now is shared.</p>}
      {session.controlUnavailableReason && <p role="status" className="text-warn">{unavailableReason[session.controlUnavailableReason]}</p>}
      {session.workspace?.changed && <p role="status" className="text-warn">Files changed outside this session. It will re-read them before your next follow-up.</p>}
    </div>}
    {/* A prompt the Mac holds for a remote answer gets the approval card; any other waits in the terminal. */}
    <SessionApprovalCard session={session} client={client} own={own} ownerName={ownerName} />
    {waitingOn !== null && !session.pendingApproval && <div role="status" className="local-session-waiting">
      <p className="font-semibold">{own ? "Waiting on you in your terminal" : `Waiting on ${ownerName} in their terminal`}</p>
      {waitingOn && <p className="local-session-waiting-reason">{waitingOn}</p>}
      {own ? <p className="text-sm">Answer it in your terminal; this prompt can&apos;t be answered from here.</p> : messageOwner}
    </div>}
    <div className="local-session-scroll">
      <div ref={scroller} className="room-context-body local-session-history" role="region" aria-label="Native session history" tabIndex={0} onScroll={onScroll}>
        <div ref={content}>
          {history.loading && <div className="native-session-shimmer" role="status" aria-label="Loading session history" />}
          <NativeSessionTranscript provider={session.provider} events={events} busy={busy && !history.hasNewer} ownerName={ownerName} participants={people}
            status={session.status} pending={pendingText && !history.hasNewer ? { text: pendingText, chip: command.chip } : null}
            outbox={savedOutbox.map(receipt => ({
              requestId: receipt.requestId, text: receipt.text ?? "",
              chip: receiptChip({ request: { requestId: receipt.requestId, expectedGeneration: receipt.expectedGeneration, kind: "send", text: receipt.text ?? "" }, state: "received", receipt }, agentName),
              onCancel: outbox.canCancel && receipt.state === "pending" ? () => void outbox.cancel(receipt.requestId) : undefined,
            }))} />
          {savedOutbox.some(receipt => receipt.state === "pending" || receipt.state === "dispatched" || receipt.state === "unknown") && <Button type="button" variant="outline" size="sm" onClick={() => void outbox.refresh()}>Check queued delivery</Button>}
          {history.error && <div role="alert"><p>{history.error}</p><Button variant="outline" onClick={() => void history.retry()}>Retry history</Button></div>}
          {!history.loading && !history.error && history.events.length === 0 && !pendingText && <p>No shared output yet.</p>}

        </div>
      </div>
    </div>
    <SubagentPanel provider={session.provider} events={history.events} status={session.status} onPick={id => revealSubagent(content.current, id)} />
    {session.status === "ended" ? null : control ? <form className="room-composer local-session-composer native-session-composer" onSubmit={event => { event.preventDefault(); atBottomRef.current = true; command.send(text); }}>
      <label htmlFor={id} className="sr-only">Send to {target}</label>
      <div className="native-session-prompt-row"><span className="native-session-glyph" aria-hidden="true">{glyph}</span>
        <textarea id={id} className="room-input" rows={2} value={text} disabled={!command.canSend || command.locked} onChange={event => setText(event.target.value)}
          onKeyDown={event => { if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing && event.keyCode !== 229) { event.preventDefault(); event.currentTarget.form?.requestSubmit(); } }}
          placeholder="Send a follow-up…" />
        <Button type="submit" size="icon-sm" className="room-send" aria-label="Send follow-up" disabled={!command.canSend || command.locked || !text.trim()}><Send aria-hidden="true" /></Button></div>
      {footer && <p className="native-session-footer" title="Set in the terminal. It cannot be changed from here.">{footer}</p>}
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
        <p className="text-sm text-muted-foreground">{join.state === "declined" ? `${ownerName} declined your request.` : `Only ${ownerName} can send to this session.`}</p>
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

/** The owner answers "Ask to join" in the session itself, where a request's notification lands. */
type JoinDecider = { decideLocalSessionJoin?: (sessionId: string, userId: string, decision: "allow" | "decline", requestId?: string) => Promise<unknown> };
function JoinRequests({ session, client, personName }: { session: LocalSession; client: JoinDecider; personName: (who: string) => string }) {
  // One entry per decision in flight, so finishing one never re-enables another's buttons.
  const [busy, setBusy] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const decide = async (who: string, decision: "allow" | "decline") => {
    if (!client.decideLocalSessionJoin) return;
    setBusy(list => [...list, who]); setError(null);
    try { await client.decideLocalSessionJoin(session.sessionId, who, decision, session.pendingJoinIds?.[who]); }
    catch { setError("Could not answer that request. Try again."); }
    finally { setBusy(list => list.filter(x => x !== who)); }
  };
  return <div className="session-notices">
    {session.pendingJoins?.map(who => <div key={who} role="status" className="local-session-join-request">
      <p>{personName(who)} wants to join this session.</p>
      <div className="local-session-actions">
        <Button type="button" size="sm" disabled={busy.includes(who)} onClick={() => void decide(who, "allow")}>Allow</Button>
        <Button type="button" variant="outline" size="sm" disabled={busy.includes(who)} onClick={() => void decide(who, "decline")}>Decline</Button>
      </div>
    </div>)}
    {error && <p role="alert" className="text-warn">{error}</p>}
  </div>;
}
