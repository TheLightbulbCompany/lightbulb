"use client";

import { useId, useState } from "react";
import { useAuth } from "@clerk/nextjs";
import { ShieldCheck } from "lucide-react";
import { approvalAgent, approvalInputView, approvalModeAction, canControlSession, useSessionApproval, useShownInputMatches, type LocalSession, type SessionControlClient } from "@lightbulb/shared";
import { Button } from "@/components/ui/button";
import { Diff } from "./NativeSessionTranscript";

/** Hex SHA-256, the digest the Mac binds an approval to. */
async function sha256Hex(text: string): Promise<string> {
  const bytes = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return Array.from(new Uint8Array(bytes), b => b.toString(16).padStart(2, "0")).join("");
}

/** The call as the Mac shipped it, every key of it: a command and its flags, each edit apart, or the whole JSON. */
function CallInput({ tool, input }: { tool: string; input: Record<string, unknown> }) {
  const pre = "my-2 max-h-72 overflow-y-auto whitespace-pre-wrap break-all rounded bg-muted p-2 font-mono text-caption";
  const view = approvalInputView(tool, input);
  if (view.kind === "json") return <pre className={pre}>{view.text}</pre>;
  if (view.kind === "bash") return <div className="my-2 text-caption">
    {view.outsideSandbox && <p className="font-medium text-warn">Runs outside the sandbox</p>}
    <pre className={pre}>{view.command}</pre>
    {view.notes.map(note => <p key={note} className="whitespace-pre-wrap break-all text-muted-foreground">{note}</p>)}
  </div>;
  return <div className="my-2 text-caption">
    <p className="whitespace-pre-wrap break-all font-mono">{view.path}</p>
    {view.edits.map((edit, i) => <div key={i} className={i ? "mt-2 border-t pt-2" : ""}>
      {view.edits.length > 1 && <p className="font-medium">Edit {i + 1} of {view.edits.length}</p>}
      {edit.everyOccurrence && <p className="font-medium">Replaces every occurrence</p>}
      <Diff lines={edit.lines} truncated={false} />
    </div>)}
  </div>;
}

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
  const { pending, answer, state } = useSessionApproval(client, session, () => crypto.randomUUID(), sha256Hex);
  const digestOk = useShownInputMatches(session.pendingApproval, sha256Hex);
  // Per prompt: a new prompt never opens mid-way through denying the last one.
  const [denyingFor, setDenyingFor] = useState<string | null>(null);
  const [reason, setReason] = useState("");
  const reasonId = useId();
  const { userId } = useAuth();
  if (!pending || !["claude", "codex"].includes(session.provider) || session.status !== "waiting-approval") return null;
  const allowed = canControlSession(session, own, userId);
  const canApprove = pending.exact || (pending.full === true && digestOk);
  const denying = denyingFor === pending.approvalId;
  const current = state?.approvalId === pending.approvalId ? state : null;
  const busy = current?.status === "sending" || current?.status === "sent";
  const where = own ? "your" : `${ownerName}'s`;
  return <section className="mx-3 my-2 rounded-md border border-warn p-3 text-ui" aria-label="Permission prompt">
    <p className="font-medium">{approvalAgent(session.provider)} wants to use {pending.tool}</p>
    {pending.input ? <CallInput tool={pending.tool} input={pending.input} />
      : pending.summary && <pre className="my-2 whitespace-pre-wrap break-all rounded bg-muted p-2 font-mono text-caption">{pending.summary}</pre>}
    {!allowed ? <p className="text-muted-foreground">Waiting on {where} approval in the terminal.</p> : <>
      {!canApprove && <p className="text-muted-foreground">Lightbulb shows only part of this call. Approve it on {where} Mac; you can deny it here.</p>}
      {denying ? <form className="mt-2 flex flex-col gap-2" onSubmit={event => { event.preventDefault(); void answer({ decision: "deny", reason }); }}>
        <label htmlFor={reasonId}>Tell {approvalAgent(session.provider)} why (optional)</label>
        <input id={reasonId} className="room-input" maxLength={500} value={reason} onChange={event => setReason(event.target.value)} />
        <div className="flex gap-2">
          <Button type="submit" variant="outline" size="sm" disabled={busy}>Deny</Button>
          <Button type="button" variant="ghost" size="sm" onClick={() => setDenyingFor(null)}>Back</Button>
        </div>
      </form> : <div className="mt-2 flex flex-wrap gap-2">
        {canApprove && <Button size="sm" disabled={busy} onClick={() => void answer({ decision: "allow" })}><ShieldCheck aria-hidden="true" />Approve</Button>}
        {canApprove && pending.modes.map(mode => <Button key={mode} size="sm" variant="outline" disabled={busy}
          onClick={() => void answer({ decision: "allow", mode })}>{approvalModeAction(mode, pending)}</Button>)}
        <Button size="sm" variant="outline" disabled={busy} onClick={() => setDenyingFor(pending.approvalId)}>Deny…</Button>
      </div>}
      {current?.status === "sending" && <p role="status" className="mt-2">Submitting your decision…</p>}
      {current?.status === "sent" && <p role="status" className="mt-2">Sent to {where} Mac.</p>}
      {current?.error && <p role="alert" className="mt-2 text-warn">{current.error}</p>}
    </>}
  </section>;
}
