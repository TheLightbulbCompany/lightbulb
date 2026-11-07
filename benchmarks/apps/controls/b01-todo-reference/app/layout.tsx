import "./globals.css";
import type { ReactNode } from "react";
import Link from "next/link";
import { SignOutButton } from "@/components/sign-out-button";
import { currentUser } from "@/lib/auth";

export const metadata = { title: "Tasklet" };

export default async function RootLayout({ children }: { children: ReactNode }) {
  const user = await currentUser();
  return (
    <html lang="en">
      <body className="min-h-screen bg-slate-50 text-slate-900">
        <header className="flex items-center justify-between border-b bg-white px-6 py-3">
          <Link href="/tasks" className="font-semibold">Tasklet</Link>
          {user && (
            <div className="flex items-center gap-3 text-sm">
              <span data-testid="user-menu">{user.name}</span>
              <SignOutButton />
            </div>
          )}
        </header>
        <main className="mx-auto max-w-2xl p-6">{children}</main>
      </body>
    </html>
  );
}
