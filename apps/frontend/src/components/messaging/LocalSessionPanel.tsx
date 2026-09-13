"use client";

import { useEffect, useId, useRef, useState } from "react";
import { ApiError, accessLost, useLocalSessionHistory, type LocalSession, type LocalSessionCommand, type LocalSessionCommandReceipt, type RoomResourceSource } from "@lightbulb/shared";
import { Button } from "@/components/ui/button";
import { copyText, sessionPermalink } from "./messageLinks";
import { LocalSessionWork } from "./LocalSessionWork";
import { NativeSessionTranscript, type Person } from "./NativeSessionTranscript";

export const sessionLabel = (s: LocalSession) => `${s.provider === "claude" ? "Claude" : "Codex"} · ${s.title?.trim() || s.sessionId.slice(0, 8)}`;
export const sessionStatus: Record<LocalSession["status"], string> = {
  idle: "Idle", busy: "Working", "waiting-approval": "Waiting for approval on owner's machine", unknown: "Activity unknown", offline: "Offline",
};
const unavailable: Record<NonNullable<LocalSession["controlUnavailableReason"]>, string> = {
  "runtime-unverified": "This session's native runtime has not been verified.",
  "endpoint-unavailable": "The native input connection is unavailable.",
  "unsupported-version": "This provider version does not support verified control.",
  "history-gap": "History has a gap. Control is unavailable until the desktop reconnects safely.",
};
type Attempt = { request: LocalSessionCommand; state: "sending" | "uncertain" | "rejected" | "received"; receipt?: LocalSessionCommandReceipt; error?: string };

export function LocalSessionPanel({ session, ownerName, roomId, client, revision, onAccessLost, onShare, participants = [] }: {
  session: LocalSession; ownerName: string; roomId: string; client: RoomResourceSource["client"];
  revision: number; onAccessLost: () => void; onShare?: (url: string) => void;
  participants?: readonly Person[];
}) {
  const busy = session.status === "busy";
  const history = useLocalSessionHistory(client, roomId, session.sessionId, session.latestEventSeq, revision, onAccessLost, { busy });
  const [text, setText] = useState("");
  const [attempt, setAttempt] = useState<Attempt | null>(null);
  const [checking, setChecking] = useState(false);
  const [copyStatus, setCopyStatus] = useState("");
  const alive = useRef(true);
  const pending = useRef(false);
  const id = useId();
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  const canSend = session.status !== "offline" && session.capabilitiesVerified &&
    !session.controlUnavailableReason && session.capabilities.sendMode !== "none" &&
    !(session.status === "busy" && session.capabilities.sendMode === "immediate" && !session.activeTurnId);
  const unknownCurrent = attempt?.receipt?.state === "unknown" && attempt.receipt.expectedGeneration === session.generation;
  const locked = checking || attempt?.state === "sending" || attempt?.state === "uncertain" || unknownCurrent;

  async function submit(request: LocalSessionCommand) {
    if (pending.current || !canSend) return;
    pending.current = true;
    setAttempt({ request, state: "sending" });
    try {
      const receipt = await client.submitLocalSessionCommand(roomId, session.sessionId, request);
      if (!alive.current) return;
      setAttempt({ request, state: "received", receipt });
      setText("");
    } catch (e) {
      if (!alive.current) return;
      if (accessLost(e)) { onAccessLost(); return; }
      const rejected = e instanceof ApiError && e.status < 500;
      setAttempt({ request, state: rejected ? "rejected" : "uncertain", error: rejected
        ? e.status === 409 ? "Session changed or is offline. Refresh before sending again."
          : "The session rejected this request. Check its availability and capabilities."
        : "Delivery unknown. Check delivery or retry the same request; it may already have been admitted." });
    } finally { pending.current = false; }
  }
  async function checkDelivery() {
    if (!attempt || pending.current) return;
    pending.current = true;
    setChecking(true);
    try {
      const receipt = await client.getLocalSessionCommand(roomId, session.sessionId, attempt.request.requestId);
      if (alive.current) {
        setAttempt({ ...attempt, state: "received", receipt, error: undefined });
        if (attempt.state === "uncertain") setText("");
      }
    } catch (e) {
      if (!alive.current) return;
      // A missing receipt can mean the original request never arrived. Do not resend implicitly.
      if (e instanceof ApiError && e.status === 403) { onAccessLost(); return; }
      setAttempt({ ...attempt, error: "Delivery is still unconfirmed. Checking does not resend the request." });
    } finally { pending.current = false; if (alive.current) setChecking(false); }
  }
  const receipt = attempt?.receipt;
  const receiptText = receipt?.state === "unknown" ? "Delivery unknown. The request may have been admitted. Further remote input remains paused until this is resolved or the native session restarts. A restart does not undo an admitted command; do not send a duplicate."
    : receipt?.held ? "Queued or held by the native session."
    : receipt?.state === "applied" ? "Admitted to the native session — not completed work."
      : receipt?.state === "pending" ? "Pending delivery to the desktop."
        : receipt?.state === "dispatched" ? "Dispatched; native admission is not yet confirmed."
          : receipt?.state === "rejected" ? "The desktop rejected this request." : null;
  const target = sessionLabel(session);
  const glyph = session.provider === "claude" ? ">" : "›";
  return <>
    <div className="local-session-summary native-session-header">
      <p className="native-session-title"><span className={`native-session-dot native-session-dot-${session.status}`} aria-hidden="true" /><span className="font-semibold">{target}</span>{session.cwdName && <span className="text-sm text-muted-foreground"> · ~/{session.cwdName}</span>}</p>
      <p className="text-sm text-muted-foreground">{ownerName} · {sessionStatus[session.status]}{session.status === "waiting-approval" ? ` on ${ownerName}'s Mac` : ""}</p>
      <p className="text-sm text-muted-foreground">Follow-ups {session.capabilities.sendMode === "queue" ? "queue" : "wait"} on {ownerName}&apos;s Mac. Approvals stay in their terminal.</p>
      {session.controlUnavailableReason && <p role="status" className="text-sm text-warn">{unavailable[session.controlUnavailableReason]}</p>}
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
    <form className="room-composer local-session-composer native-session-composer" onSubmit={event => {
      event.preventDefault();
      if (locked || !text.trim() || !canSend) return;
      const request: LocalSessionCommand = { requestId: crypto.randomUUID(), expectedGeneration: session.generation, kind: "send", text: text.trim(),
        ...(session.status === "busy" && session.capabilities.sendMode === "immediate" ? { expectedTurnId: session.activeTurnId } : {}) };
      if (new TextEncoder().encode(JSON.stringify({ ...request, expectedTurnId: request.expectedTurnId ?? null })).length > 8192) {
        setAttempt({ request, state: "rejected", error: "This follow-up is too long. Shorten it to fit the 8 KiB request limit." });
        return;
      }
      void submit(request);
    }}>
      <label htmlFor={id} className="text-sm font-semibold">Send to {target}</label>
      <div className="native-session-prompt-row"><span className="native-session-glyph" aria-hidden="true">{glyph}</span>
        <textarea id={id} className="room-input" rows={2} value={text} disabled={!canSend || locked} onChange={event => setText(event.target.value)}
          onKeyDown={event => { if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing && event.keyCode !== 229) { event.preventDefault(); event.currentTarget.form?.requestSubmit(); } }}
          placeholder="Ask this session a follow-up… (Enter sends, Shift+Enter newline)" /></div>
      {!canSend && <p className="text-sm text-muted-foreground">Sending is unavailable for this session.</p>}
      <Button type="submit" disabled={!canSend || locked || !text.trim()}>Send follow-up</Button>
      {attempt?.state === "sending" && <p role="status">Sending follow-up…</p>}
      {attempt?.error && <p role="alert" className="text-sm text-warn">{attempt.error}</p>}
      {receiptText && <p role="status" className="text-sm">{receipt?.actor ? `Agent ${receipt.actor.id}: ` : ""}{receiptText}</p>}
      {receipt?.held && <p className="text-sm text-muted-foreground">Admission is not completed work.</p>}
      {(attempt?.state === "uncertain" || (receipt && ["pending", "dispatched", "unknown"].includes(receipt.state))) && <Button type="button" variant="outline" disabled={checking} onClick={() => void checkDelivery()}>Check delivery</Button>}
      {attempt?.state === "uncertain" && <Button type="button" variant="outline" disabled={!canSend || checking} onClick={() => void submit(attempt.request)}>Retry same request</Button>}
    </form>
  </>;
}
