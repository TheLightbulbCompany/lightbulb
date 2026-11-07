import type { Metadata } from "next";
import Link from "next/link";

import { Button } from "@/components/ui/button";

// =============================================================================
// Where a workspace invitation email lands: /join/invite?workspace=…&inviter=…
//
// Clerk appends `__clerk_ticket` (the grant) and `__clerk_status` (`sign_up`
// for a new address, `sign_in` for an existing account, `complete` once used)
// to the redirect URL the backend set in `workspace_invitations.send`. This
// page only explains why the person is here and hands Clerk's ticket and status on
// to /sign-up or /sign-in, whose prebuilt Clerk forms consume the ticket exactly
// as they did when the email linked there directly. The two names are
// display-only text from that same URL; nothing here grants anything.
// =============================================================================

export const metadata: Metadata = {
  title: "You're invited",
  robots: { index: false, follow: false },
};

type Search = Record<string, string | string[] | undefined>;

const one = (value: string | string[] | undefined): string =>
  (Array.isArray(value) ? value[0] : value)?.trim().slice(0, 120) ?? "";

function inviteHeadline(inviter: string, workspace: string): string {
  const who = inviter || "Your teammate";
  return workspace ? `${who} invited you to ${workspace}.` : `${who} invited you to their workspace.`;
}

export default async function InvitationPage({ searchParams }: { searchParams: Promise<Search> }) {
  const search = await searchParams;
  const query = new URLSearchParams();
  // Only what Clerk's form needs: its ticket and status. The names stay on this page.
  for (const key of ["__clerk_ticket", "__clerk_status"]) {
    const raw = search[key];
    const v = Array.isArray(raw) ? raw[0] : raw; // never truncated: a ticket is a long JWT
    if (v) query.set(key, v);
  }
  // T09: an invitation from an approval card lands the new teammate on Activity, where Needs you lists it.
  const approval = /^[A-Za-z0-9_-]{1,64}$/.test(one(search.approval)) ? one(search.approval) : "";
  const decision = one(search.decision);
  if (approval) {
    query.set("redirect_url", "/chat?panel=activity");
  }
  const qs = query.toString() ? `?${query}` : "";
  const status = one(search.__clerk_status);
  const headline = approval && decision
    ? `${one(search.inviter) || "Your teammate"} asked you to decide: ${decision}`
    : inviteHeadline(one(search.inviter), one(search.workspace));

  return (
    <main className="mx-auto flex min-h-screen max-w-md flex-col items-center justify-center gap-4 px-6 text-center">
      <h1 className="font-serif text-2xl text-midnight">{headline}</h1>
      {status === "complete" ? (
        <>
          <p className="text-ui text-midnight-muted">You already accepted this invitation.</p>
          <Button asChild><Link href="/chat">Open Lightbulb</Link></Button>
        </>
      ) : (
        <>
          <p className="text-ui text-midnight-muted">
            {approval && decision
              ? "Sign up to see it and answer. It opens the moment you're in."
              : "Sign up to see what the team and its AI employees are working on."}
          </p>
          {status === "sign_in" ? (
            <Button asChild><Link href={`/sign-in${qs}`}>Sign in to join</Link></Button>
          ) : (
            <>
              <Button asChild><Link href={`/sign-up${qs}`}>Sign up to join</Link></Button>
              <p className="text-ui text-midnight-muted">
                Already have an account? <Link className="underline" href={`/sign-in${qs}`}>Sign in</Link>
              </p>
            </>
          )}
        </>
      )}
    </main>
  );
}
