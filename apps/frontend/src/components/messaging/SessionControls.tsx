"use client";

import { useEffect, useId, useState } from "react";
import { useAuth } from "@clerk/nextjs";
import { ShieldCheck } from "lucide-react";
import { APPROVAL_MODE_LABEL, canControlSession, useNativeSendApproval, useSessionApproval, type LocalSession, type PrivateSendApproval, type SessionControlClient } from "@lightbulb/shared";
import { Button } from "@/components/ui/button";

type Client = SessionControlClient;

/**
 * A permission prompt the session's Mac is holding open, answerable here by its
 * owner -- and by any workspace member while the owner lets teammates control
 * the session. The answer reaches the terminal through the desktop; answering
 * in the terminal first simply closes this card.
 */
export function SessionApprovalCard({ session, client, own, ownerName }: {
  session: LocalSession; client: Client; own: boolean; ownerName: string;
}) {
  const { pending, answer, state } = useSessionApproval(client, session, () => crypto.randomUUID());
  const native = useNativeSendApproval(client, session.sessionId);
  // Per prompt: a new prompt never opens mid-way through denying the last one.
  const [denyingFor, setDenyingFor] = useState<string | null>(null);
  const [reason, setReason] = useState("");
  const [detail, setDetail] = useState<PrivateSendApproval | null>(null);
  const [failedFor, setFailedFor] = useState<string | null>(null);
  const [retry, setRetry] = useState(0);
  const reasonId = useId();
  const { userId } = useAuth();
  const privateSend = pending?.tool === "mcp__lightbulb_native_sessions__send";
  const nativeSend = !!session.nativeSendApproval && privateSend;
  const approvalId = pending?.approvalId;
  const inputDigest = pending?.inputDigest;
  const detailKey = `${approvalId}:${inputDigest}`;
  useEffect(() => {
    if (!own || !privateSend || !approvalId || !inputDigest || !client.getPrivateSendApproval) return;
    let active = true;
    void client.getPrivateSendApproval(session.sessionId)
      .then((value) => { if (active) { setDetail(value); setFailedFor(null); } })
      .catch(() => { if (active) setFailedFor(detailKey); });
    return () => { active = false; };
  }, [client, own, privateSend, approvalId, inputDigest, detailKey, retry, session.sessionId]);
  if (!pending || session.status === "ended" || (session.provider === "claude" && session.status !== "waiting-approval" && !nativeSend)) return null;
  if (session.provider === "codex" && !nativeSend) return null;
  const allowed = canControlSession(session, own, userId) && (!privateSend || own);
  const shown = own && privateSend && detail?.approvalId === pending.approvalId && detail.inputDigest === pending.inputDigest ? detail : null;
  const canApprove = privateSend ? !!shown && (!nativeSend || session.status !== "offline") : pending.exact;
  const denying = denyingFor === pending.approvalId;
  const last = nativeSend ? native.state : state;
  const current = last?.approvalId === pending.approvalId ? last : null;
  const busy = current?.status === "sending" || current?.status === "sent";
  const where = own ? "your" : `${ownerName}'s`;
  const respond = (decision: "allow" | "deny" | "cancel") => {
    if (nativeSend) {
      if (shown) void native.answer(shown, decision, reason);
    } else if (decision !== "cancel") {
      void answer({ decision, reason });
    }
  };
  return <section className="mx-3 my-2 rounded-md border border-warn p-3 text-sm" aria-label={nativeSend ? `${session.provider === "claude" ? "Claude" : "Codex"} send approval` : "Permission prompt"}>
    <p className="font-medium">{nativeSend ? `${session.provider === "claude" ? "Claude" : "Codex"} wants to send a Lightbulb message` : `${session.provider === "claude" ? "Claude" : "Codex"} wants to use ${pending.tool}`}</p>
    {pending.summary && <pre className="my-2 whitespace-pre-wrap break-all rounded bg-muted p-2 font-mono text-xs">{pending.summary}</pre>}
    {shown && <div className="my-2 rounded bg-muted p-2 text-xs"><p className="font-medium">To: {shown.destinationLabel || shown.destination}</p><p className="text-muted-foreground">{shown.destinationLabel ? `${shown.destination} · ` : ""}Request ID: {shown.requestId}</p><pre className="mt-2 max-h-72 overflow-y-auto whitespace-pre-wrap break-words font-mono">{shown.text}</pre></div>}
    {!allowed ? <p className="text-muted-foreground">Waiting on {where} approval{privateSend ? "." : " in the terminal."}</p> : <>
      {!canApprove && <p className="text-muted-foreground">{nativeSend && session.status === "offline"
        ? "Your Mac is offline. This message cannot be sent; you can deny or cancel it."
        : privateSend
        ? failedFor === detailKey ? nativeSend ? "Could not load the exact message. Retry when connected." : "Could not load the exact message. Retry, or approve on your Mac." : "Loading the exact message…"
        : <>Approve this in the terminal on {where} Mac: Lightbulb can&apos;t show all of it. You can still deny it here.</>}</p>}
      {privateSend && !shown && failedFor === detailKey && <Button size="sm" variant="outline" onClick={() => { setFailedFor(null); setRetry(value => value + 1); }}>Retry loading message</Button>}
      {denying ? <form className="mt-2 flex flex-col gap-2" onSubmit={event => { event.preventDefault(); respond("deny"); }}>
        <label htmlFor={reasonId}>Tell {session.provider === "claude" ? "Claude" : "Codex"} why (optional)</label>
        <input id={reasonId} className="room-input" maxLength={500} value={reason} onChange={event => setReason(event.target.value)} />
        <div className="flex gap-2">
          <Button type="submit" variant="outline" size="sm" disabled={busy || (nativeSend && !shown)}>Deny</Button>
          <Button type="button" variant="ghost" size="sm" onClick={() => setDenyingFor(null)}>Back</Button>
        </div>
      </form> : <div className="mt-2 flex flex-wrap gap-2">
        {canApprove && <Button size="sm" disabled={busy} onClick={() => respond("allow")}><ShieldCheck aria-hidden="true" />Approve</Button>}
        {!nativeSend && canApprove && pending.modes.map(mode => <Button key={mode} size="sm" variant="outline" disabled={busy}
          onClick={() => void answer({ decision: "allow", mode })}>Approve and switch to {APPROVAL_MODE_LABEL[mode]}</Button>)}
        <Button size="sm" variant="outline" disabled={busy} onClick={() => setDenyingFor(pending.approvalId)}>Deny…</Button>
        {nativeSend && shown && <Button size="sm" variant="outline" disabled={busy} onClick={() => respond("cancel")}>Cancel send</Button>}
      </div>}
      {current?.status === "sending" && <p role="status" className="mt-2">Submitting your decision…</p>}
      {current?.status === "sent" && <p role="status" className="mt-2">{nativeSend ? "Decision recorded." : `Sent to ${where} Mac.`}</p>}
      {current?.error && <p role="alert" className="mt-2 text-warn">{current.error}</p>}
    </>}
  </section>;
}
