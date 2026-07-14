"use client";

import Link from "next/link";
import { useClerk, useUser } from "@clerk/nextjs";

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
      <h1 className="text-2xl font-semibold text-zinc-100">403 — Not authorized</h1>
      <p className="max-w-md text-zinc-400">
        {email ? (
          <>
            Signed in as <span className="text-zinc-200">{email}</span> — this account is not a
            platform admin. Sign out and switch to an admin account.
          </>
        ) : (
          "You are not a platform admin."
        )}
      </p>
      <div className="flex items-center gap-3">
        <button
          onClick={() => signOut({ redirectUrl: "/sign-in" })}
          className="rounded-md bg-zinc-100 px-4 py-2 text-sm font-medium text-zinc-900 hover:bg-white"
        >
          Sign out / switch account
        </button>
        <Link
          href="/"
          className="rounded-md border border-zinc-700 bg-zinc-900 px-4 py-2 text-sm text-zinc-200 hover:bg-zinc-800"
        >
          Back to home
        </Link>
      </div>
    </div>
  );
}
