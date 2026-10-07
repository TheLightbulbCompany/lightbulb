"use client";

import { useId, useState } from "react";
import { sessionLabel, sessionStatus, unavailableReason, useLocalSessionHistory, useSessionCommand, type LocalSession, type RoomResourceSource } from "@lightbulb/shared";
import { Button } from "@/components/ui/button";
import { copyText, sessionPermalink } from "./messageLinks";
import { LocalSessionWork } from "./LocalSessionWork";
import { NativeSessionTranscript, type Person } from "./NativeSessionTranscript";

export function LocalSessionPanel({ session, ownerName, own, roomId, client, revision, onAccessLost, onShare, participants = [] }: {
  session: LocalSession; ownerName: string; own: boolean; roomId: string; client: RoomResourceSource["client"];
  revision: number; onAccessLost: () => void; onShare?: (url: string) => void;
  participants?: readonly Person[];
}) {
  const busy = session.status === "busy";
  // Backstop poll while the session is producing output: the relay throttles
  // transcript hints (1/s per session, no trailing edge), so hints alone can
  // leave an open transcript behind during sustained output. Idle sessions
  // stay on hints plus the resources safety poll. Quiet: no spinner, no focus steal.
  const history = useLocalSessionHistory(client, roomId, session.sessionId, session.latestEventSeq, revision, onAccessLost, { pollMs: busy ? 5000 : 0 });
  const [text, setText] = useState("");
  const [copyStatus, setCopyStatus] = useState("");
  const id = useId();
  const command = useSessionCommand(client, roomId, session, { uuid: () => crypto.randomUUID(), onAccessLost, onSent: () => setText("") });
  const target = sessionLabel(session);
  const glyph = session.provider === "claude" ? ">" : "›";
  return <>
    <div className="local-session-summary native-session-header">
      <p className="native-session-title"><span className={`native-session-dot native-session-dot-${session.status}`} aria-hidden="true" /><span className="font-semibold">{target}</span>{session.cwdName && <span className="text-sm text-muted-foreground"> · ~/{session.cwdName}</span>}</p>
      <p className="text-sm text-muted-foreground">{ownerName} · {sessionStatus[session.status]}{session.status === "waiting-approval" ? ` on ${own ? "your" : `${ownerName}'s`} Mac` : ""}</p>
      <p className="text-sm text-muted-foreground">Follow-ups {session.capabilities.sendMode === "queue" ? "queue" : "wait"} on {own ? "your" : `${ownerName}'s`} Mac. Approvals stay in {own ? "your" : "their"} terminal.</p>
      {session.controlUnavailableReason && <p role="status" className="text-sm text-warn">{unavailableReason[session.controlUnavailableReason]}</p>}
      {session.workspace?.changed && <p role="status" className="text-sm text-warn">Checkout changes detected. The next remote contribution asks the session to re-read relevant files; refresh is not proven.</p>}
      <div className="flex flex-wrap gap-2">
        <Button variant="outline" onClick={async () => setCopyStatus(await copyText(sessionPermalink(roomId, session.sessionId)) ? "Session link copied." : "Could not copy session link.")}>Copy session link</Button>
        {onShare && <Button variant="outline" onClick={() => onShare(sessionPermalink(roomId, session.sessionId))}>Add link to conversation draft</Button>}
        <LocalSessionWork roomId={roomId} sessionId={session.sessionId} />
      </div>
      {copyStatus && <p role="status">{copyStatus}</p>}
      <details className="text-sm text-muted-foreground"><summary>About this session</summary>
        <p>{session.providerVersion}. {session.workspace?.overlap ? "Multiple sessions may overlap this checkout. Coordinate edits to avoid conflicts. " : ""}{session.workspace?.coverage === "bounded" ? "Ignored, sensitive, oversized and unsampled files are not covered. Changes have external/unknown provenance." : "Workspace change coverage is unknown. Re-read relevant files before continuing."}</p>
        <p>Output arrives block by block as the harness records it, not token by token. Current members of the owner&apos;s other eligible conversations can see this shared transcript too. Contributions use the owner&apos;s native permissions and provider usage. Approval stays on their machine; room history is never sent automatically.</p>
      </details>
    </div>
    <div className="room-context-body local-session-history" role="region" aria-label="Native session history" tabIndex={0}>
      {history.hasOlder && <Button variant="outline" disabled={history.loading} onClick={() => void history.load("older")}>Load older</Button>}
      <NativeSessionTranscript provider={session.provider} events={history.events} busy={busy && !history.hasNewer} ownerName={ownerName} participants={participants} />
      {history.loading && <p role="status">Loading session history…</p>}
      {history.error && <div role="alert"><p>{history.error}</p><Button variant="outline" onClick={() => void history.retry()}>Retry history</Button></div>}
      {!history.loading && !history.error && history.events.length === 0 && <p>No shared output yet.</p>}
      {history.hasNewer && <div className="local-session-pagination"><p className="text-sm text-muted-foreground">More recent activity is available.</p><Button variant="outline" disabled={history.loading} onClick={() => void history.load("newer")}>Load newer</Button><Button variant="outline" disabled={history.loading} onClick={() => void history.load("latest")}>Jump to latest</Button></div>}
    </div>
    <form className="room-composer local-session-composer native-session-composer" onSubmit={event => { event.preventDefault(); command.send(text); }}>
      <label htmlFor={id} className="text-sm font-semibold">Send to {target}</label>
      <div className="native-session-prompt-row"><span className="native-session-glyph" aria-hidden="true">{glyph}</span>
        <textarea id={id} className="room-input" rows={2} value={text} disabled={!command.canSend || command.locked} onChange={event => setText(event.target.value)}
          onKeyDown={event => { if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing && event.keyCode !== 229) { event.preventDefault(); event.currentTarget.form?.requestSubmit(); } }}
          placeholder="Ask this session a follow-up… (Enter sends, Shift+Enter newline)" /></div>
      {!command.canSend && <p className="text-sm text-muted-foreground">Sending is unavailable for this session.</p>}
      <Button type="submit" disabled={!command.canSend || command.locked || !text.trim()}>Send follow-up</Button>
      {command.attempt?.state === "sending" && <p role="status">Sending follow-up…</p>}
      {command.attempt?.error && <p role="alert" className="text-sm text-warn">{command.attempt.error}</p>}
      {command.receiptText && <p role="status" className="text-sm">{command.receiptText}</p>}
      {command.receipt?.held && <p className="text-sm text-muted-foreground">Admission is not completed work.</p>}
      {command.canCheck && <Button type="button" variant="outline" disabled={command.checking} onClick={() => void command.checkDelivery()}>Check delivery</Button>}
      {command.attempt?.state === "uncertain" && <Button type="button" variant="outline" disabled={!command.canSend || command.checking} onClick={() => command.retry()}>Retry same request</Button>}
    </form>
  </>;
}
