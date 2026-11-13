import { redirect } from "next/navigation";
import { auth } from "@clerk/nextjs/server";
import Link from "next/link";

import { NotAuthorized } from "@/components/admin/NotAuthorized";

import { getAdminMe } from "./_lib/api";

/**
 * Admin tree gate (Server Component).
 *
 * Order of checks:
 *   1. Clerk auth — redirect to /sign-in if not signed in.
 *   2. Backend `/admin/me` — single source of truth for admin membership.
 *      Anything other than `is_admin: true` renders the NotAuthorized screen
 *      IN PLACE (not a redirect — see the note at the check below).
 *
 * The whole admin route segment sits behind this layout, so individual pages
 * don't need their own gate. Pair with the host-based 404 in
 * `src/middleware.ts` for defense in depth (CEO A1).
 */
export default async function AdminLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const { userId, getToken } = await auth();
  if (!userId) {
    redirect("/sign-in");
  }

  const token = await getToken();
  if (!token) {
    redirect("/sign-in");
  }

  const me = await getAdminMe(token);
  if (!me?.is_admin) {
    // Render the not-authorized screen IN PLACE — do NOT redirect. The
    // /admin/not-authorized route is itself behind this layout, so
    // redirecting there re-runs this gate and redirects again forever
    // (ERR_TOO_MANY_REDIRECTS). Rendering inline breaks the loop and gives
    // the non-admin a sign-out / switch-account button.
    return (
      <div className="dark min-h-screen bg-background text-foreground">
        <NotAuthorized />
      </div>
    );
  }

  return (
    <div className="dark min-h-screen bg-background text-foreground">
      <header className="border-b border-border bg-card">
        <div className="mx-auto flex max-w-7xl items-center justify-between px-6 py-4">
          <div className="flex items-center gap-8">
            <Link href="/admin" className="text-lg font-semibold text-foreground">
              Lightbulb admin
            </Link>
            <nav className="flex items-center gap-1 text-sm">
              <Link
                href="/admin"
                className="rounded-md px-3 py-1.5 text-muted-foreground hover:bg-accent hover:text-foreground"
              >
                Overview
              </Link>
              <Link
                href="/admin/users"
                className="rounded-md px-3 py-1.5 text-muted-foreground hover:bg-accent hover:text-foreground"
              >
                Users
              </Link>
              <Link
                href="/admin/growth"
                className="rounded-md px-3 py-1.5 text-muted-foreground hover:bg-accent hover:text-foreground"
              >
                Growth
              </Link>
              <Link
                href="/admin/deploys"
                className="rounded-md px-3 py-1.5 text-muted-foreground hover:bg-accent hover:text-foreground"
              >
                Deploys
              </Link>
              <Link
                href="/admin/audit"
                className="rounded-md px-3 py-1.5 text-muted-foreground hover:bg-accent hover:text-foreground"
              >
                Audit
              </Link>
              <Link
                href="/admin/health"
                className="rounded-md px-3 py-1.5 text-muted-foreground hover:bg-accent hover:text-foreground"
              >
                Health
              </Link>
            </nav>
          </div>
          <div className="text-xs text-muted-foreground">
            {me.email ?? me.user_id}
          </div>
        </div>
      </header>
      <main className="mx-auto max-w-7xl px-6 py-8">{children}</main>
    </div>
  );
}
