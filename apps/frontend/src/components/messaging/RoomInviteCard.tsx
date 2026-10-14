"use client";

import { useState, useSyncExternalStore, type FormEvent } from "react";
import { useAuth } from "@clerk/nextjs";
import { useApi } from "@/lib/api";
import { JOIN_LINK_COPIED, joinLinkNote, useCopyJoinLink } from "@/hooks/useCopyJoinLink";
import { inviteSendNote } from "@/components/workspace/InvitePeoplePanel";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

const dismissKey = (orgId: string) => `lightbulb.inviteCard.dismissed.${orgId}`;
const listeners = new Set<() => void>();

function useDismissed(orgId: string | null | undefined): [boolean, () => void] {
  const dismissed = useSyncExternalStore(
    (l) => { listeners.add(l); return () => { listeners.delete(l); }; },
    () => !!orgId && window.localStorage.getItem(dismissKey(orgId)) === "1",
    () => false,
  );
  return [dismissed, () => {
    if (!orgId) return;
    window.localStorage.setItem(dismissKey(orgId), "1");
    listeners.forEach((l) => l());
  }];
}

/** The inline invite a `/chat?panel=invite` link renders as (the CEO's "Who should see this with
 *  you?", 08 §3 screen 6): one email + Send, Copy join link, and "Just me for now". Any member may
 *  invite (D5). */
export function RoomInviteCard({ source = "chat", offerJustMe = true }: { source?: string; offerJustMe?: boolean } = {}) {
  const { orgId } = useAuth();
  const api = useApi();
  const copyJoinLink = useCopyJoinLink();
  const [dismissed, dismiss] = useDismissed(orgId);
  const [email, setEmail] = useState("");
  const [busy, setBusy] = useState(false);
  const [sentTo, setSentTo] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);

  if (!orgId) return null;
  if (dismissed && offerJustMe) return <p className="text-sm text-muted-foreground">You can invite people any time from Get started.</p>;
  if (sentTo) return <p role="status" className="text-sm">Invite sent to {sentTo}. They&apos;ll land in this channel.</p>;

  async function send(event: FormEvent) {
    event.preventDefault();
    const address = email.trim();
    if (!address || busy) return;
    setBusy(true);
    setNote(null);
    try {
      await api.post(`/orgs/${orgId}/invitations`, { email: address, source });
      setSentTo(address);
    } catch (error) {
      setNote(inviteSendNote(error));
    } finally {
      setBusy(false);
    }
  }

  function copy() {
    if (busy) return;
    setBusy(true);
    setNote(null);
    copyJoinLink(source).then(() => setNote(JOIN_LINK_COPIED), (error: unknown) => setNote(joinLinkNote(error)))
      .finally(() => setBusy(false));
  }

  return (
    <section aria-label="Invite someone" className={offerJustMe ? "editorial-card flex max-w-xl flex-col gap-2 p-3" : "flex flex-col gap-2"}>
      <form onSubmit={send} className="flex flex-wrap gap-2">
        <Input aria-label="Email address" type="email" value={email} onChange={(e) => setEmail(e.target.value)}
          placeholder="name@company.com" className="min-w-0 flex-1" autoComplete="off" spellCheck={false} autoCapitalize="none" />
        <Button type="submit" disabled={busy || !email.trim()}>Send invite</Button>
      </form>
      <div className="flex flex-wrap gap-2">
        <Button type="button" variant="outline" size="sm" disabled={busy} onClick={copy}>Copy join link</Button>
        {offerJustMe && <Button type="button" variant="ghost" size="sm" onClick={dismiss}>Just me for now</Button>}
      </div>
      {note && <p role="status" className="text-sm text-muted-foreground">{note}</p>}
    </section>
  );
}
