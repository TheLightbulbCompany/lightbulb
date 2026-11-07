import "./globals.css";
import type { ReactNode } from "react";
import Link from "next/link";
import { SignOutButton } from "@/components/sign-out-button";
import { currentUser } from "@/lib/auth";
import { TZ } from "@/lib/time";

export const metadata = { title: "Slotline" };

export default async function RootLayout({ children }: { children: ReactNode }) {
  const user = await currentUser();
  return (
    <html lang="en">
      <body className="min-h-screen bg-slate-50 text-slate-900">
        <header className="flex items-center justify-between border-b bg-white px-6 py-3">
          <nav className="flex items-center gap-4">
            <Link href="/bookings" className="font-semibold">Slotline</Link>
            {user && (
              <>
                <Link href="/bookings" data-testid="nav-bookings" className="underline">Bookings</Link>
                <Link href="/practitioners" className="underline">Practitioners</Link>
                <Link href="/services" className="underline">Services</Link>
              </>
            )}
          </nav>
          <div className="flex items-center gap-3 text-sm">
            <span data-testid="clinic-timezone" className="text-slate-500">{TZ}</span>
            {user && (
              <>
                <span data-testid="user-menu">{user.name}</span>
                <SignOutButton />
              </>
            )}
          </div>
        </header>
        <main className="mx-auto max-w-2xl p-6">{children}</main>
      </body>
    </html>
  );
}
