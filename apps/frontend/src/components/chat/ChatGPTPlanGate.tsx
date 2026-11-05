"use client";

// ChatGPT sign-in comes with the Workspace plan (routers/oauth.py /start refuses 402 otherwise). Every ChatGPT entry
// point renders its sign-in through this gate, so a free workspace never starts the OpenAI device-code flow: it picks
// the plan, goes through Checkout, and comes straight back into ChatGPT sign-in.

import { useEffect, useState, type ReactNode } from "react";
import { Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useBilling } from "@/hooks/useBilling";

/** Renders `children` (the sign-in) only on a paid plan. `waitForPlan`: just back from Checkout, so wait out the
 *  Stripe webhook (up to 30 s) before offering the plan again. */
export function ChatGPTPlanGate({ waitForPlan = false, children }: { waitForPlan?: boolean; children: ReactNode }) {
  const { account, isLoading, isSubscribed, refresh } = useBilling();
  const [waiting, setWaiting] = useState(waitForPlan);
  useEffect(() => {
    if (!waiting || isSubscribed) return;
    const poll = setInterval(() => void refresh(), 2000);
    const giveUp = setTimeout(() => setWaiting(false), 30_000);
    return () => { clearInterval(poll); clearTimeout(giveUp); };
  }, [waiting, isSubscribed, refresh]);
  if (isSubscribed) return <>{children}</>;
  if ((isLoading && !account) || waiting) {
    return <div role="status" className="flex flex-col items-center gap-3 py-6 text-sm text-[var(--midnight-muted)]">
      <Loader2 size={20} className="animate-spin" aria-hidden="true" />
      {waiting ? "Confirming your plan…" : "Checking your plan…"}
    </div>;
  }
  return <ChatGPTPlanFirst />;
}

/** The plan step before ChatGPT sign-in: one sentence and one button; Checkout returns to the sign-in. */
export function ChatGPTPlanFirst() {
  const { startCheckout } = useBilling();
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  return <div className="space-y-3 py-4">
    <p className="text-sm text-[var(--midnight)]">Signing in with ChatGPT comes with the Workspace plan.</p>
    <p className="text-sm text-[var(--midnight-muted)]">Choose the plan first. After checkout you come straight back here to sign in with ChatGPT.</p>
    <Button type="button" disabled={pending} onClick={async () => {
      setPending(true);
      setError(null);
      try { await startCheckout("workspace", false, "chatgpt"); }
      catch { setError("Checkout didn't open. Try again."); setPending(false); }
    }}>{pending ? "Opening checkout…" : "Choose the Workspace plan"}</Button>
    {error && <p role="alert" className="text-sm text-[var(--err)]">{error}</p>}
  </div>;
}
