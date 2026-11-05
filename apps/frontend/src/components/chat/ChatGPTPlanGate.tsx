"use client";

// ChatGPT sign-in comes with the Workspace plan (routers/oauth.py /start refuses 402 otherwise). Every ChatGPT entry
// point renders its sign-in through this gate, so a free workspace never starts the OpenAI device-code flow: it picks
// the plan, goes through Checkout, and comes straight back into ChatGPT sign-in.

import { useEffect, useState, type ReactNode } from "react";
import { useSearchParams } from "next/navigation";
import { Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useBilling } from "@/hooks/useBilling";

/** Where Checkout returns to (routers/billing.py `then`): Settings → Model, or the onboarding ChatGPT step. */
export type ChatGPTReturn = "chatgpt" | "chatgpt_onboarding";

const WEBHOOK_WAIT_MS = 30_000;

/** Renders `children` (the sign-in) only on a paid plan. Back from Checkout (`waitForPlan`, or `?checkout=success` on
 *  mount) it waits for the Stripe webhook and never offers the plan again: a slow webhook reads "still being
 *  confirmed" with Check again. */
export function ChatGPTPlanGate({ waitForPlan = false, then = "chatgpt", children }: {
  waitForPlan?: boolean; then?: ChatGPTReturn; children: ReactNode;
}) {
  const { account, isLoading, isSubscribed, refresh } = useBilling();
  const searchParams = useSearchParams();
  // Read once on mount: the shell strips ?checkout= right after.
  const [paid] = useState(() => waitForPlan || searchParams.get("checkout") === "success");
  const [waitRound, setWaitRound] = useState(0);
  const [waiting, setWaiting] = useState(paid);
  useEffect(() => {
    if (!waiting || isSubscribed) return;
    const poll = setInterval(() => void refresh(), 2000);
    const giveUp = setTimeout(() => setWaiting(false), WEBHOOK_WAIT_MS);
    return () => { clearInterval(poll); clearTimeout(giveUp); };
  }, [waiting, isSubscribed, refresh, waitRound]);
  if (isSubscribed) return <>{children}</>;
  if ((isLoading && !account) || waiting) {
    return <div role="status" className="flex flex-col items-center gap-3 py-6 text-sm text-[var(--midnight-muted)]">
      <Loader2 size={20} className="animate-spin" aria-hidden="true" />
      {waiting ? "Confirming your plan…" : "Checking your plan…"}
    </div>;
  }
  if (paid) {
    // Just paid, webhook still pending: never sell the plan again (Checkout would refuse an existing subscription).
    return <div role="status" className="space-y-3 py-4">
      <p className="text-sm text-[var(--midnight)]">Your payment is still being confirmed.</p>
      <p className="text-sm text-[var(--midnight-muted)]">This usually takes a few seconds. ChatGPT sign-in starts as soon as it lands.</p>
      <Button type="button" variant="outline" onClick={() => { void refresh(); setWaiting(true); setWaitRound((n) => n + 1); }}>Check again</Button>
    </div>;
  }
  return <ChatGPTPlanFirst then={then} />;
}

/** The plan step before ChatGPT sign-in: one sentence and one button; Checkout returns to the sign-in. */
export function ChatGPTPlanFirst({ then = "chatgpt" }: { then?: ChatGPTReturn }) {
  const { startCheckout } = useBilling();
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  return <div className="space-y-3 py-4">
    <p className="text-sm text-[var(--midnight)]">Signing in with ChatGPT comes with the Workspace plan.</p>
    <p className="text-sm text-[var(--midnight-muted)]">Choose the plan first. After checkout you come straight back here to sign in with ChatGPT.</p>
    <Button type="button" disabled={pending} onClick={async () => {
      setPending(true);
      setError(null);
      try { await startCheckout("workspace", false, then); }
      catch { setError("Checkout didn't open. Try again."); setPending(false); }
    }}>{pending ? "Opening checkout…" : "Choose the Workspace plan"}</Button>
    {error && <p role="alert" className="text-sm text-[var(--err)]">{error}</p>}
  </div>;
}
