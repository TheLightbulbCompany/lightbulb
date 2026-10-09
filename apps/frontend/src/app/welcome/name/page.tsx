"use client";

import { useEffect, useState, type FormEvent } from "react";
import { useRouter } from "next/navigation";
import { useUser } from "@clerk/nextjs";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { needsName } from "@/hooks/useNameGate";
import { safeReturnTo } from "@/lib/returnTo";

/** Where useNameGate sends a signed-in person Clerk has no name for. Anyone who
 *  already has one (or reloads after saving) goes straight on, so it cannot loop. */
export default function NamePage() {
  const router = useRouter();
  const { isLoaded, user } = useUser();
  const [first, setFirst] = useState("");
  const [last, setLast] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const done = isLoaded && !!user && !needsName(user);

  useEffect(() => {
    if (!isLoaded) return;
    if (!user) router.replace("/sign-in");
    else if (done) router.replace(safeReturnTo(new URLSearchParams(window.location.search).get("returnTo")));
  }, [isLoaded, user, done, router]);

  async function save(event: FormEvent) {
    event.preventDefault();
    if (!user || busy || !first.trim()) return;
    setBusy(true);
    setError(null);
    try {
      // Clerk refreshes `user`, and the effect above takes them back.
      await user.update({ firstName: first.trim(), lastName: last.trim() });
    } catch {
      setError("We couldn't save your name. Please try again.");
      setBusy(false);
    }
  }

  if (!isLoaded || !user || done) {
    return (
      <main className="flex min-h-screen items-center justify-center bg-background p-4">
        <div className="settings-spinner" role="status" aria-label="Loading" />
      </main>
    );
  }

  return (
    <main className="flex min-h-screen flex-col items-center justify-center gap-6 bg-background p-4">
      <h1 className="text-2xl font-bold">What should people call you?</h1>
      <p>Your teammates and agents see this name next to everything you send.</p>
      <form onSubmit={save} className="flex w-full max-w-md flex-col gap-4 rounded-xl border border-border bg-card p-6 shadow-sm">
        <label className="flex flex-col gap-1.5 text-sm font-medium">
          First name
          <Input value={first} onChange={(e) => setFirst(e.target.value)} autoFocus autoComplete="given-name" maxLength={64} required />
        </label>
        <label className="flex flex-col gap-1.5 text-sm font-medium">
          Last name (optional)
          <Input value={last} onChange={(e) => setLast(e.target.value)} autoComplete="family-name" maxLength={64} />
        </label>
        {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
        <Button type="submit" disabled={busy || !first.trim()} className="self-end">
          {busy ? "Saving…" : "Continue"}
        </Button>
      </form>
    </main>
  );
}
