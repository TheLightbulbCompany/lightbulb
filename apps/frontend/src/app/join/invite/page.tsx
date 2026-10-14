import type { Metadata } from "next";
import Link from "next/link";

import { Button } from "@/components/ui/button";

// =============================================================================
// Where a workspace invitation email lands: /join/invite?workspace=…&inviter=…
//
// Clerk appends `__clerk_ticket` (the grant) and `__clerk_status` (`sign_up`
// for a new address, `sign_in` for an existing account, `complete` once used)
// to the redirect URL the backend set in `workspace_invitations.send`. This
// page only explains why the person is here and hands the WHOLE query on to
// /sign-up or /sign-in, whose prebuilt Clerk forms consume the ticket exactly
// as they did when the email linked there directly. The two names are
// display-only text from that same URL; nothing here grants anything.
// =============================================================================

export const metadata: Metadata = {
  title: "You're invited",
  robots: { index: false, follow: false },
};

type Search = Record<string, string | string[] | undefined>;

const one = (value: string | string[] | undefined): string =>
  (Array.isArray(value) ? value[0] : value)?.trim().slice(0, 80) ?? "";

function inviteHeadline(inviter: string, workspace: string): string {
  const who = inviter || "Your teammate";
  return workspace ? `${who} invited you to ${workspace}.` : `${who} invited you to their workspace.`;
}

export default async function InvitationPage({ searchParams }: { searchParams: Promise<Search> }) {
  const search = await searchParams;
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(search)) {
    for (const v of Array.isArray(value) ? value : value === undefined ? [] : [value]) query.append(key, v);
  }
  const qs = query.toString() ? `?${query}` : "";
  const status = one(search.__clerk_status);

  return (
    <main className="mx-auto flex min-h-screen max-w-md flex-col items-center justify-center gap-4 px-6 text-center">
      <h1 className="font-serif text-2xl text-midnight">{inviteHeadline(one(search.inviter), one(search.workspace))}</h1>
      {status === "complete" ? (
        <>
          <p className="text-ui text-midnight-muted">You already accepted this invitation.</p>
          <Button asChild><Link href="/chat">Open Lightbulb</Link></Button>
        </>
      ) : (
        <>
          <p className="text-ui text-midnight-muted">
            Sign up to see what the team and its AI employees are working on.
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
