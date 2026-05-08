import { redirect } from "next/navigation";
import { auth } from "@clerk/nextjs/server";
import Link from "next/link";

import { getAdminMe } from "./_lib/api";

/**
 * Admin tree gate (Server Component).
 *
 * Order of checks:
 *   1. Clerk auth — redirect to /sign-in if not signed in.
 *   2. Backend `/admin/me` — single source of truth for admin membership.
 *      Anything other than `is_admin: true` redirects to /admin/not-authorized.
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
    redirect("/admin/not-authorized");
  }

  return (
    <div className="dark min-h-screen bg-background text-foreground">
      <header className="border-b border-zinc-800 bg-zinc-900">
        <div className="mx-auto flex max-w-7xl items-center justify-between px-6 py-4">
          <div className="flex items-center gap-8">
            <Link href="/admin" className="text-lg font-semibold text-zinc-100">
              isol8 admin
            </Link>
            <nav className="flex items-center gap-1 text-sm">
              <Link
                href="/admin/users"
                className="rounded-md px-3 py-1.5 text-zinc-300 hover:bg-zinc-800 hover:text-zinc-100"
              >
                Users
              </Link>
              <Link
                href="/admin/marketplace/listings"
                className="rounded-md px-3 py-1.5 text-zinc-300 hover:bg-zinc-800 hover:text-zinc-100"
              >
                Marketplace
              </Link>
              <Link
                href="/admin/health"
                className="rounded-md px-3 py-1.5 text-zinc-300 hover:bg-zinc-800 hover:text-zinc-100"
              >
                Health
              </Link>
            </nav>
          </div>
          <div className="text-xs text-zinc-500">
            {me.email ?? me.user_id}
          </div>
        </div>
      </header>
      <main className="mx-auto max-w-7xl px-6 py-8">{children}</main>
    </div>
  );
}
