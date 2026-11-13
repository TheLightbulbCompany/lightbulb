"use client";

import { useEffect, useState, type FormEvent } from "react";
import { useAuth } from "@clerk/nextjs";
import { ApiError, useApi } from "@/lib/api";
import { JOIN_LINK_COPIED, joinLinkNote, useCopyJoinLink } from "@/hooks/useCopyJoinLink";
import { timeAgo } from "@/lib/timeAgo";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

/** `GET /orgs/{orgId}/invitations` rows. Clerk sends the email; the link lands on /join/invite. */
export type Invitation = { id: string; email: string; created_at: string; expires_at: string | null; status: "pending" };
type SendResult = { email: string; sent: boolean; note: string };

/** One line per address. A 409's detail is already a sentence ("…already in this workspace."). */
export function inviteSendNote(error: unknown): string {
  if (error instanceof ApiError) {
    const detail = (error.body as { detail?: unknown } | undefined)?.detail;
    if (error.status === 409 && typeof detail === "string" && detail) return detail;
    if (error.status === 422) return "That doesn't look like an email address.";
    if (error.status === 403) return "You can't invite people to this workspace.";
  }
  return "Couldn't send. Try again.";
}

/** The one invite UI (the rail's Invite dialog and Settings → Members): email invitations, the
 *  pending list, and the join link for people who would rather get a link in a chat. Any member
 *  may invite and copy the link (D5); the pending list and Revoke are admin-only, so a member's
 *  403 on the list just hides that section. */
/** `onJoinLinkCopied` fires after a successful copy, which may have MINTED the link: a host that
 *  shows its own join-link state (Settings → Members) reloads it, so its card never offers
 *  "Create join link" over a link the admin just copied (that POST would rotate the token). */
export function InvitePeoplePanel({ source, onJoinLinkCopied }: { source: string; onJoinLinkCopied?: () => void }): React.ReactElement {
  const { orgId } = useAuth();
  const api = useApi();
  const copyJoinLink = useCopyJoinLink();
  const [pending, setPending] = useState<Invitation[] | null>(null);
  const [listState, setListState] = useState<"loading" | "ready" | "forbidden" | "error">("loading");
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [results, setResults] = useState<SendResult[]>([]);
  const [revokeError, setRevokeError] = useState<string | null>(null);
  const [copying, setCopying] = useState(false); // one mint at a time: a second POST replaces the first link
  const [linkNote, setLinkNote] = useState<string | null>(null);

  useEffect(() => {
    if (!orgId) return;
    let stale = false;
    api.get(`/orgs/${orgId}/invitations`).then(
      (body) => { if (!stale) { setPending((body as { invitations: Invitation[] }).invitations); setListState("ready"); } },
      (error: unknown) => { if (!stale) setListState(error instanceof ApiError && error.status === 403 ? "forbidden" : "error"); });
    return () => { stale = true; };
  }, [api, orgId]);

  if (listState === "loading") return <p role="status" className="text-sm text-muted-foreground">Loading…</p>;

  async function send(event: FormEvent) {
    event.preventDefault();
    const emails = [...new Set(draft.split(/[\s,;]+/).filter(Boolean))];
    if (!emails.length || sending || !orgId) return;
    setSending(true);
    setResults([]);
    const settled = await Promise.all(emails.map(async (email): Promise<SendResult> => {
      try {
        const invitation = (await api.post(`/orgs/${orgId}/invitations`, { email, source })) as Invitation;
        setPending((current) => [invitation, ...(current ?? []).filter((row) => row.id !== invitation.id)]);
        return { email, sent: true, note: "Invitation sent." };
      } catch (error) {
        return { email, sent: false, note: inviteSendNote(error) };
      }
    }));
    setResults(settled);
    setDraft(settled.filter((result) => !result.sent).map((result) => result.email).join(", ")); // what failed stays to fix
    setSending(false);
  }

  async function revoke(invitation: Invitation) {
    const index = pending?.findIndex((row) => row.id === invitation.id) ?? -1;
    setRevokeError(null);
    setPending((current) => current?.filter((row) => row.id !== invitation.id) ?? current);
    try {
      await api.del(`/orgs/${orgId}/invitations/${invitation.id}`);
    } catch {
      setPending((current) => { const next = [...(current ?? [])]; next.splice(Math.max(0, index), 0, invitation); return next; });
      setRevokeError(`Couldn't revoke the invitation to ${invitation.email}. Try again.`);
    }
  }

  function copyLink() {
    if (copying) return;
    setCopying(true);
    setLinkNote(null);
    copyJoinLink(source).then(() => { setLinkNote(JOIN_LINK_COPIED); onJoinLinkCopied?.(); }, (error: unknown) => setLinkNote(joinLinkNote(error)))
      .finally(() => setCopying(false));
  }

  return (
    <div className="flex flex-col gap-5">
      <form onSubmit={send} className="flex flex-col gap-2" aria-busy={sending}>
        <label htmlFor="invite-emails" className="text-sm font-medium">Email address</label>
        <div className="flex flex-wrap gap-2">
          <Input id="invite-emails" value={draft} onChange={(e) => setDraft(e.target.value)} placeholder="name@company.com, another@company.com"
            className="min-w-0 flex-1" autoComplete="off" spellCheck={false} autoCapitalize="none" />
          <Button type="submit" disabled={sending || !draft.trim()}>{sending ? "Sending…" : "Send invites"}</Button>
        </div>
        <p className="text-sm text-muted-foreground">Separate several addresses with commas. Each person gets an email with a link to join.</p>
        {results.length > 0 && <ul aria-label="Invite results" className="flex flex-col gap-1 text-sm">
          {results.map((result) => <li key={result.email} className={result.sent ? "text-foreground" : "text-destructive"}>
            <span className="font-medium">{result.email}</span>: {result.note}</li>)}
        </ul>}
      </form>

      {listState !== "forbidden" && <section aria-labelledby="invite-pending" className="flex flex-col gap-2">
        <h3 id="invite-pending" className="text-sm font-medium">Pending invitations</h3>
        {listState === "error" ? <p className="text-sm text-muted-foreground">Couldn&apos;t load pending invitations.</p>
          : !pending?.length ? <p className="text-sm text-muted-foreground">No pending invitations.</p>
            : <ul className="flex flex-col">{pending.map((invitation) => <li key={invitation.id} className="flex items-center gap-3 border-b border-border py-2 last:border-b-0">
              <span className="min-w-0 flex-1 truncate text-sm">{invitation.email}</span>
              <span className="text-sm text-muted-foreground">Sent {timeAgo(invitation.created_at)}</span>
              <Button type="button" variant="outline" size="sm" aria-label={`Revoke invitation to ${invitation.email}`} onClick={() => void revoke(invitation)}>Revoke</Button>
            </li>)}</ul>}
        {revokeError && <p role="alert" className="text-sm text-destructive">{revokeError}</p>}
      </section>}

      <section className="flex flex-col gap-1.5 border-t border-border pt-4">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <span className="text-sm">Or share the join link</span>
          <Button type="button" variant="outline" size="sm" disabled={copying} onClick={copyLink}>Copy join link</Button>
        </div>
        {linkNote && <p role="status" className="text-sm text-muted-foreground">{linkNote}</p>}
      </section>
    </div>
  );
}
