"use client";

import { useState, type FormEvent } from "react";
import { useOrganization } from "@clerk/nextjs";
import { useApi } from "@/lib/api";
import { inviteSendNote } from "@/components/workspace/InvitePeoplePanel";
import { EditorialButton } from "@/components/teams/shell";

/** T09: on an approval card in a one-person workspace, "Ask someone else to decide" invites a
 *  teammate by email. The invitation names the decision and opens this card after sign-up. */
export function AskSomeoneToDecide({ approvalId, decision }: { approvalId: string; decision: string }) {
  const { organization } = useOrganization();
  const api = useApi();
  const [open, setOpen] = useState(false);
  const [email, setEmail] = useState("");
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const [sentTo, setSentTo] = useState<string | null>(null);

  if (!organization || organization.membersCount !== 1) return null;
  if (sentTo) return <p role="status" className="text-ui">Asked {sentTo}. The card opens for them as soon as they sign up.</p>;
  if (!open) {
    return (
      <button type="button" className="self-start text-ui underline text-[var(--midnight-muted)]" onClick={() => setOpen(true)}>
        Ask someone else to decide
      </button>
    );
  }

  async function send(event: FormEvent) {
    event.preventDefault();
    const address = email.trim();
    if (!address || busy || !organization) return;
    setBusy(true);
    setNote(null);
    try {
      await api.post(`/orgs/${organization.id}/invitations`, {
        email: address, source: "approval", approval_id: approvalId, decision,
      });
      setSentTo(address);
    } catch (error) {
      setNote(inviteSendNote(error));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={send} className="flex flex-wrap items-center gap-2" aria-label="Ask someone else to decide">
      <input
        type="email"
        aria-label="Their email"
        placeholder="name@company.com"
        value={email}
        onChange={(e) => setEmail(e.target.value)}
        className="flex-1 min-w-0 bg-transparent"
        style={{ borderBottom: "1px solid var(--edge)", color: "var(--midnight)" }}
      />
      <EditorialButton size="sm" type="submit" disabled={busy || !email.trim()}>Send invite</EditorialButton>
      {note && <p role="status" className="w-full text-ui text-[var(--midnight-muted)]">{note}</p>}
    </form>
  );
}
