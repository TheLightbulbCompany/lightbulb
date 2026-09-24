"use client";

import { useId, useRef, useState } from "react";
import { Info, Link, ListTodo, MoreHorizontal, Send } from "lucide-react";
import { sessionLabel, sessionStatus, unavailableReason, useLocalSessionHistory, useSessionCommand, type LocalSession, type SessionSource } from "@lightbulb/shared";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from "@/components/ui/dropdown-menu";
import { copyText, sessionPermalink } from "./messageLinks";
import { LocalSessionWork } from "./LocalSessionWork";
import { NativeSessionTranscript } from "./NativeSessionTranscript";

/** The session's secondary actions -- copy link, linked work, about -- behind one ⋯ in the focus header. */
export function SessionMenu({ session, ownerName, own }: { session: LocalSession; ownerName: string; own: boolean }) {
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
        <DropdownMenuItem onSelect={() => setDialog("work")}><ListTodo aria-hidden="true" />Linked work</DropdownMenuItem>
        <DropdownMenuItem onSelect={() => setDialog("about")}><Info aria-hidden="true" />About this session</DropdownMenuItem>
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
        <p>Follow-ups {session.capabilities.sendMode === "queue" ? "queue" : "wait"} on {own ? "your" : `${ownerName}'s`} Mac. Approvals stay in {own ? "your" : "their"} terminal.</p>
        <p>{session.providerVersion}. {session.workspace?.overlap ? "Multiple sessions may overlap this checkout. Coordinate edits to avoid conflicts. " : ""}{session.workspace?.coverage === "bounded" ? "Ignored, sensitive, oversized and unsampled files are not covered. Changes have external/unknown provenance." : "Workspace change coverage is unknown. Re-read relevant files before continuing."}</p>
        <p>Output arrives block by block as the harness records it, not token by token. Current members of the owner&apos;s other eligible conversations can see this shared transcript too. Contributions use the owner&apos;s native permissions and provider usage. Approval stays on their machine; room history is never sent automatically.</p>
      </DialogContent>
    </Dialog>
  </>;
}

export function LocalSessionPanel({ session, ownerName, own, client, revision, onAccessLost }: {
  session: LocalSession; ownerName: string; own: boolean; client: SessionSource["client"];
  revision: number; onAccessLost: () => void;
}) {
  const busy = session.status === "busy";
  // Backstop poll while the session is producing output: the relay throttles
  // transcript hints (1/s per session, no trailing edge), so hints alone can
  // leave an open transcript behind during sustained output. Idle sessions
  // stay on hints plus the sessions safety poll. Quiet: no spinner, no focus steal.
  const history = useLocalSessionHistory(client, session.sessionId, session.latestEventSeq, revision, onAccessLost, { pollMs: busy ? 5000 : 0 });
  const [text, setText] = useState("");
  const id = useId();
  const command = useSessionCommand(client, session, { uuid: () => crypto.randomUUID(), onAccessLost, onSent: () => setText("") });
  const target = sessionLabel(session);
  const glyph = session.provider === "claude" ? ">" : "›";
  return <>
    <div className="local-session-summary native-session-header">
      <p className="native-session-title text-sm text-muted-foreground"><span className={`native-session-dot native-session-dot-${session.status}`} aria-hidden="true" /><span className="room-wrap">{session.cwdName && `~/${session.cwdName} · `}{ownerName} · {sessionStatus[session.status]}{session.status === "waiting-approval" ? ` on ${own ? "your" : `${ownerName}'s`} Mac` : ""}</span></p>
      {session.controlUnavailableReason && <p role="status" className="text-sm text-warn">{unavailableReason[session.controlUnavailableReason]}</p>}
      {session.workspace?.changed && <p role="status" className="text-sm text-warn">Checkout changes detected. The next remote contribution asks the session to re-read relevant files; refresh is not proven.</p>}
    </div>
    <div className="room-context-body local-session-history" role="region" aria-label="Native session history" tabIndex={0}>
      {history.hasOlder && <Button variant="outline" disabled={history.loading} onClick={() => void history.load("older")}>Load older</Button>}
      <NativeSessionTranscript provider={session.provider} events={history.events} busy={busy && !history.hasNewer} ownerName={ownerName} participants={[]} />
      {history.loading && <p role="status">Loading session history…</p>}
      {history.error && <div role="alert"><p>{history.error}</p><Button variant="outline" onClick={() => void history.retry()}>Retry history</Button></div>}
      {!history.loading && !history.error && history.events.length === 0 && <p>No shared output yet.</p>}
      {history.hasNewer && <div className="local-session-pagination"><p className="text-sm text-muted-foreground">More recent activity is available.</p><Button variant="outline" disabled={history.loading} onClick={() => void history.load("newer")}>Load newer</Button><Button variant="outline" disabled={history.loading} onClick={() => void history.load("latest")}>Jump to latest</Button></div>}
    </div>
    {session.status === "ended" ? null : own ? <form className="room-composer local-session-composer native-session-composer" onSubmit={event => { event.preventDefault(); command.send(text); }}>
      <label htmlFor={id} className="sr-only">Send to {target}</label>
      <div className="native-session-prompt-row"><span className="native-session-glyph" aria-hidden="true">{glyph}</span>
        <textarea id={id} className="room-input" rows={2} value={text} disabled={!command.canSend || command.locked} onChange={event => setText(event.target.value)}
          onKeyDown={event => { if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing && event.keyCode !== 229) { event.preventDefault(); event.currentTarget.form?.requestSubmit(); } }}
          placeholder="Send a follow-up…" />
        <Button type="submit" size="icon-sm" className="room-send" aria-label="Send follow-up" disabled={!command.canSend || command.locked || !text.trim()}><Send aria-hidden="true" /></Button></div>
      {!command.canSend && <p className="text-sm text-muted-foreground">Sending is unavailable for this session.</p>}
      {command.attempt?.state === "sending" && <p role="status">Sending follow-up…</p>}
      {command.attempt?.error && <p role="alert" className="text-sm text-warn">{command.attempt.error}</p>}
      {command.receiptText && <p role="status" className="text-sm">{command.receiptText}</p>}
      {command.canCheck && <Button type="button" variant="outline" disabled={command.checking} onClick={() => void command.checkDelivery()}>Check delivery</Button>}
      {command.attempt?.state === "uncertain" && <Button type="button" variant="outline" disabled={!command.canSend || command.checking} onClick={() => command.retry()}>Retry same request</Button>}
    </form> : <p className="room-composer text-sm text-muted-foreground">Only {ownerName} can send to this session.</p>}
  </>;
}
