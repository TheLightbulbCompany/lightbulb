"use client";

import { useId, useState } from "react";
import { useAuth } from "@clerk/nextjs";
import { ShieldCheck } from "lucide-react";
import { APPROVAL_MODE_LABEL, canControlSession, useSessionApproval, type LocalSession, type SessionControlClient } from "@lightbulb/shared";
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
  // Per prompt: a new prompt never opens mid-way through denying the last one.
  const [denyingFor, setDenyingFor] = useState<string | null>(null);
  const [reason, setReason] = useState("");
  const reasonId = useId();
  const { userId } = useAuth();
  if (session.status !== "waiting-approval" || !pending) return null;
  const allowed = canControlSession(session, own, userId);
  const denying = denyingFor === pending.approvalId;
  const busy = state?.status === "sending" || state?.status === "sent";
  const where = own ? "your" : `${ownerName}'s`;
  return <section className="mx-3 my-2 rounded-md border border-warn p-3 text-sm" aria-label="Permission prompt">
    <p className="font-medium">{session.provider === "claude" ? "Claude" : "Codex"} wants to use {pending.tool}</p>
    {pending.summary && <pre className="my-2 whitespace-pre-wrap break-all rounded bg-muted p-2 font-mono text-xs">{pending.summary}</pre>}
    {!allowed ? <p className="text-muted-foreground">Waiting on {where} approval in the terminal.</p> : <>
      {!pending.exact && <p className="text-muted-foreground">Approve this in the terminal on {where} Mac: Lightbulb can&apos;t show all of it. You can still deny it here.</p>}
      {denying ? <form className="mt-2 flex flex-col gap-2" onSubmit={event => { event.preventDefault(); void answer({ decision: "deny", reason }); }}>
        <label htmlFor={reasonId}>Tell {session.provider === "claude" ? "Claude" : "Codex"} why (optional)</label>
        <input id={reasonId} className="room-input" maxLength={500} value={reason} onChange={event => setReason(event.target.value)} />
        <div className="flex gap-2">
          <Button type="submit" variant="outline" size="sm" disabled={busy}>Deny</Button>
          <Button type="button" variant="ghost" size="sm" onClick={() => setDenyingFor(null)}>Back</Button>
        </div>
      </form> : <div className="mt-2 flex flex-wrap gap-2">
        {pending.exact && <Button size="sm" disabled={busy} onClick={() => void answer({ decision: "allow" })}><ShieldCheck aria-hidden="true" />Approve</Button>}
        {pending.exact && pending.modes.map(mode => <Button key={mode} size="sm" variant="outline" disabled={busy}
          onClick={() => void answer({ decision: "allow", mode })}>Approve and switch to {APPROVAL_MODE_LABEL[mode]}</Button>)}
        <Button size="sm" variant="outline" disabled={busy} onClick={() => setDenyingFor(pending.approvalId)}>Deny…</Button>
      </div>}
      {state?.status === "sending" && <p role="status" className="mt-2">Sending your answer…</p>}
      {state?.status === "sent" && <p role="status" className="mt-2">Sent to {where} Mac.</p>}
      {state?.error && <p role="alert" className="mt-2 text-warn">{state.error}</p>}
    </>}
  </section>;
}
