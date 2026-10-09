"use client";

import { useClerk, useUser } from "@clerk/nextjs";

import { Button } from "@/components/ui/button";
import { SITE_URL } from "@/lib/domain";

/**
 * Terminal "not a platform admin" screen.
 *
 * Rendered INLINE by AdminLayout (not via redirect). Previously the layout
 * did `redirect("/admin/not-authorized")` for non-admins — but that route is
 * itself behind AdminLayout, so the redirect target re-ran the same gate and
 * redirected again, forever (ERR_TOO_MANY_REDIRECTS). Rendering the screen in
 * place breaks the loop and gives a non-admin a real way out: sign out and
 * switch to an admin account.
 */
export function NotAuthorized() {
  const { signOut } = useClerk();
  const { user } = useUser();
  const email = user?.primaryEmailAddress?.emailAddress;

  return (
    <div className="flex min-h-[60vh] flex-col items-center justify-center gap-4 text-center">
      <h1 className="text-2xl font-semibold text-foreground">403 — Not authorized</h1>
      <p className="max-w-md text-muted-foreground">
        {email ? (
          <>
            Signed in as <span className="text-foreground">{email}</span> — this account is not a
            platform admin. Sign out and switch to an admin account.
          </>
        ) : (
          "You are not a platform admin."
        )}
      </p>
      <div className="flex items-center gap-3">
        <Button onClick={() => signOut({ redirectUrl: "/sign-in" })}>
          Sign out / switch account
        </Button>
        {/* Absolute product URL, not "/": on the admin host the middleware
            funnels every non-/admin path back to /admin, so "/" landed on this
            403 again — a loop with no way out but sign-out. */}
        <Button asChild variant="outline">
          <a href={SITE_URL}>Back to home</a>
        </Button>
      </div>
    </div>
  );
}
