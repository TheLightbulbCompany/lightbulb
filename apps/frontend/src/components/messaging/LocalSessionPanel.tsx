"use client";

import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import { ArrowDown, Eye, EyeOff, Info, Link, ListTodo, MessageSquare, MoreHorizontal, Send } from "lucide-react";
import {
  canControlSession, pendingApproval, sendBlockedReason, sessionLabel, sessionStatus, unavailableReason, useLocalSessionHistory, usePendingPrompt,
  useSessionAudit, useSessionCommand, withAuditNotices, type LocalSession, type SessionControlClient, type SessionSource,
} from "@lightbulb/shared";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from "@/components/ui/dropdown-menu";
import { copyText, sessionPermalink } from "./messageLinks";
import { LocalSessionWork } from "./LocalSessionWork";
import { NativeSessionTranscript, ReceiptChip, type Person } from "./NativeSessionTranscript";
import { SessionOverview } from "./SessionOverview";
import { SessionApprovalCard, TeamControlToggle } from "./SessionControls";

/** Distance from the bottom, in px, still counted as "at the bottom" (mobile's value). */
const BOTTOM_SLACK = 80;

/** The session's secondary actions -- copy link, linked work, about -- behind one ⋯ in the focus header. */
export function SessionMenu({ session, ownerName, own, onMessageOwner, client }: {
  session: LocalSession; ownerName: string; own: boolean; onMessageOwner?: () => void; client?: Pick<SessionSource["client"], "setLocalSessionSharing">;
}) {
  const [dialog, setDialog] = useState<"work" | "about" | null>(null);
  const [copyStatus, setCopyStatus] = useState("");
  const trigger = useRef<HTMLButtonElement>(null);
  const close = (open: boolean) => { if (!open) setDialog(null); };
  const restoreFocus = (event: Event) => { event.preventDefault(); trigger.current?.focus(); };
  return <>
    {copyStatus && <span role="status" className="text-sm text-muted-foreground">{copyStatus}</span>}
    <DropdownMenu>
      <DropdownMenuTrigger asChild><Button ref={trigger} variant="ghost" size="icon-sm" aria-label="Session options"><MoreHorizontal aria-hidden="true" /></Button></DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        <DropdownMenuItem onSelect={async () => setCopyStatus(await copyText(sessionPermalink(session.sessionId)) ? "Session link copied." : "Could not copy session link.")}><Link aria-hidden="true" />Copy session link</DropdownMenuItem>
        {!own && onMessageOwner && <DropdownMenuItem onSelect={onMessageOwner}><MessageSquare aria-hidden="true" />Message {ownerName}</DropdownMenuItem>}
        <DropdownMenuItem onSelect={() => setDialog("work")}><ListTodo aria-hidden="true" />Linked work</DropdownMenuItem>
        <DropdownMenuItem onSelect={() => setDialog("about")}><Info aria-hidden="true" />About this session</DropdownMenuItem>
        {own && client && <DropdownMenuItem onSelect={async () => {
          const shared = session.shared === false;
          try {
            await client.setLocalSessionSharing(session.sessionId, shared);
            setCopyStatus(shared ? "Sharing resumed." : "Sharing paused. Only you can see this session.");
          } catch {
            setCopyStatus("Could not change sharing. Try again.");
          }
        }}>{session.shared === false ? <><Eye aria-hidden="true" />Resume sharing</> : <><EyeOff aria-hidden="true" />Stop sharing this session</>}</DropdownMenuItem>}
      </DropdownMenuContent>
    </DropdownMenu>
    <Dialog open={dialog === "work"} onOpenChange={close}>
      <DialogContent className="max-w-[calc(100%-2rem)] sm:max-w-md max-h-[calc(100dvh-2rem)] overflow-y-auto" onCloseAutoFocus={restoreFocus}>
        <DialogHeader><DialogTitle>Linked work</DialogTitle>
          <DialogDescription>Reference an issue or project workspace for this session.</DialogDescription></DialogHeader>
        <LocalSessionWork sessionId={session.sessionId} />
      </DialogContent>
    </Dialog>
    <Dialog open={dialog === "about"} onOpenChange={close}>
      <DialogContent className="max-w-[calc(100%-2rem)] sm:max-w-md max-h-[calc(100dvh-2rem)] overflow-y-auto text-sm text-muted-foreground" onCloseAutoFocus={restoreFocus}>
        <DialogHeader><DialogTitle>About this session</DialogTitle>
          <DialogDescription>Trust and delivery details for this shared session.</DialogDescription></DialogHeader>
        <p>Follow-ups {session.capabilities.sendMode === "queue" ? "queue" : "wait"} on {own ? "your" : `${ownerName}'s`} Mac. Permission prompts can be answered here by {own ? "you" : ownerName}{session.teamControl ? " and teammates" : ""}, or in the terminal. A shell command Lightbulb cannot show in full is approved in the terminal only.</p>
        <p>{session.providerVersion}. {session.workspace?.overlap ? "Multiple sessions may overlap this checkout. Coordinate edits to avoid conflicts. " : ""}{session.workspace?.coverage === "bounded" ? "Ignored, sensitive, oversized and unsampled files are not covered. Changes have external/unknown provenance." : "Workspace change coverage is unknown. Re-read relevant files before continuing."}</p>
        <p>Output arrives block by block as the harness records it, not token by token. Current members of the owner&apos;s other eligible conversations can see this shared transcript too. Contributions use the owner&apos;s native permissions and provider usage. Approval stays on their machine; room history is never sent automatically.</p>
      </DialogContent>
    </Dialog>
  </>;
}

export function LocalSessionPanel({ session, ownerName, own, client, revision, onAccessLost, participants = [], onMessageOwner, nameOf }: {
  session: LocalSession; ownerName: string; own: boolean; client: SessionSource["client"] & SessionControlClient;
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
  const id = useId();
  const people = useMemo(() => [{ kind: "human" as const, id: session.humanUserId, displayName: ownerName }, ...participants], [session.humanUserId, ownerName, participants]);
  const agentName = useCallback((agentId: string) => people.find(p => p.id === agentId)?.displayName ?? "Agent", [people]);
  const command = useSessionCommand(client, session, { uuid: () => crypto.randomUUID(), onAccessLost, onSent: () => setText(""), agentName });
  const pendingText = usePendingPrompt(command.attempt, history.events);
  const blocked = sendBlockedReason(session);
  const waitingOn = session.status === "waiting-approval" ? pendingApproval(history.events) ?? "" : null;
  const target = sessionLabel(session);
  const control = canControlSession(session, own);
  const audit = useSessionAudit(client, session.sessionId, `${revision}:${session.latestEventSeq}:${session.teamControl}:${session.status}`);
  const events = withAuditNotices(history.events, audit, nameOf ?? (who => people.find(p => p.id === who)?.displayName ?? "A teammate"));
  const glyph = session.provider === "claude" ? ">" : "›";

  // Follow the output: stick to the bottom while the reader is there, stop the
  // moment they scroll up, and offer a pill back. Growth is watched on the
  // content box, so a tool row filling in (no new seq) still re-pins.
  const scroller = useRef<HTMLDivElement>(null);
  const content = useRef<HTMLDivElement>(null);
  const atBottomRef = useRef(true);
  const [atBottom, setAtBottom] = useState(true);
  const toBottom = useCallback(() => { const node = scroller.current; if (node) node.scrollTop = node.scrollHeight; }, []);
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
    const next = node.scrollHeight - node.scrollTop - node.clientHeight <= BOTTOM_SLACK;
    if (next !== atBottomRef.current) { atBottomRef.current = next; setAtBottom(next); }
  };
  const messageOwner = !own && onMessageOwner
    ? <Button type="button" variant="outline" size="sm" onClick={onMessageOwner}><MessageSquare aria-hidden="true" />Message {ownerName}</Button> : null;

  return <>
    <div className="local-session-summary native-session-header">
      <p className="native-session-title text-sm text-muted-foreground"><span className={`native-session-dot native-session-dot-${session.status}`} aria-hidden="true" /><span className="room-wrap">{session.cwdName && `~/${session.cwdName} · `}{ownerName} · {sessionStatus[session.status]}</span></p>
      {session.shared === false && <p role="status" className="text-sm text-muted-foreground">Sharing is paused. Only you can see this session, and nothing it does now is shared.</p>}
      {session.controlUnavailableReason && <p role="status" className="text-sm text-warn">{unavailableReason[session.controlUnavailableReason]}</p>}
      {session.workspace?.changed && <p role="status" className="text-sm text-warn">Files changed outside this session. It will re-read them before your next follow-up.</p>}
    </div>
    {/* A prompt the Mac holds for a remote answer gets the approval card; any other waits in the terminal. */}
    <SessionApprovalCard session={session} client={client} own={own} ownerName={ownerName} />
    {own && <TeamControlToggle session={session} client={client} />}
    {waitingOn !== null && !session.pendingApproval && <div role="status" className="local-session-waiting">
      <p className="font-semibold">{own ? "Waiting on you in your terminal" : `Waiting on ${ownerName} in their terminal`}</p>
      {waitingOn && <p className="local-session-waiting-reason">{waitingOn}</p>}
      {own ? <p className="text-sm">Answer it in your terminal; this prompt can&apos;t be answered from here.</p> : messageOwner}
    </div>}
    <SessionOverview events={history.events} />
    <div className="local-session-scroll">
      <div ref={scroller} className="room-context-body local-session-history" role="region" aria-label="Native session history" tabIndex={0} onScroll={onScroll}>
        <div ref={content}>
          {history.hasOlder && <Button variant="outline" disabled={history.loading} onClick={() => void history.load("older")}>Load older</Button>}
          <NativeSessionTranscript provider={session.provider} events={events} busy={busy && !history.hasNewer} ownerName={ownerName} participants={people}
            pending={pendingText && !history.hasNewer ? { text: pendingText, chip: command.chip } : null} />
          {history.loading && <p role="status">Loading session history…</p>}
          {history.error && <div role="alert"><p>{history.error}</p><Button variant="outline" onClick={() => void history.retry()}>Retry history</Button></div>}
          {!history.loading && !history.error && history.events.length === 0 && !pendingText && <p>No shared output yet.</p>}
          {history.hasNewer && <div className="local-session-pagination"><p className="text-sm text-muted-foreground">More recent activity is available.</p><Button variant="outline" disabled={history.loading} onClick={() => void history.load("newer")}>Load newer</Button><Button variant="outline" disabled={history.loading} onClick={() => void history.load("latest")}>Jump to latest</Button></div>}
        </div>
      </div>
      {!atBottom && !history.hasNewer && <Button type="button" variant="outline" className="room-new-pill" onClick={toBottom}><ArrowDown size={14} aria-hidden="true" />Jump to latest</Button>}
    </div>
    {session.status === "ended" ? null : control ? <form className="room-composer local-session-composer native-session-composer" onSubmit={event => { event.preventDefault(); atBottomRef.current = true; command.send(text); }}>
      <label htmlFor={id} className="sr-only">Send to {target}</label>
      <div className="native-session-prompt-row"><span className="native-session-glyph" aria-hidden="true">{glyph}</span>
        <textarea id={id} className="room-input" rows={2} value={text} disabled={!command.canSend || command.locked} onChange={event => setText(event.target.value)}
          onKeyDown={event => { if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing && event.keyCode !== 229) { event.preventDefault(); event.currentTarget.form?.requestSubmit(); } }}
          placeholder="Send a follow-up…" />
        <Button type="submit" size="icon-sm" className="room-send" aria-label="Send follow-up" disabled={!command.canSend || command.locked || !text.trim()}><Send aria-hidden="true" /></Button></div>
      {blocked && <p className="text-sm text-warn">{blocked}</p>}
      {command.attempt?.state === "rejected" && !command.attempt.receipt && command.attempt.error && <p role="alert" className="text-sm text-warn">{command.attempt.error}</p>}
      {/* Once the harness has echoed the prompt the pending row is gone; a warning still needs a home. */}
      {!pendingText && command.chip && (command.chip.tone === "warn" || command.chip.tone === "error") && command.attempt?.receipt && <p className="text-sm"><ReceiptChip chip={command.chip} /></p>}
      {(command.canCheck || command.attempt?.state === "uncertain") && <div className="local-session-actions">
        {command.canCheck && <Button type="button" variant="outline" size="sm" disabled={command.checking} onClick={() => void command.checkDelivery()}>Check delivery</Button>}
        {command.attempt?.state === "uncertain" && <Button type="button" variant="outline" size="sm" disabled={!command.canSend || command.checking} onClick={() => command.retry()}>Retry same request</Button>}
      </div>}
    </form> : <div className="room-composer local-session-readonly"><p className="text-sm text-muted-foreground">Only {ownerName} can send to this session.</p>{waitingOn === null && messageOwner}</div>}
  </>;
}
