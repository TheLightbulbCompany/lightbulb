"use client";

import { useAuth } from "@clerk/nextjs";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

const API = process.env.NEXT_PUBLIC_API_URL ?? "https://api.isol8.co";

/**
 * Stripe Connect onboarding "return" landing page.
 *
 * Stripe redirects here after the seller finishes (or cancels) the hosted
 * KYC flow. We poll /payouts/dashboard until the connect account is wired
 * up (dashboard_url present) and then redirect to /dashboard.
 *
 * Cap at 8 attempts (~16s). If still pending, send the seller to /dashboard
 * anyway — they'll see "Onboard for payouts" again and can resume.
 */
export default function PayoutsReturnPage() {
  const { isSignedIn, getToken } = useAuth();
  const router = useRouter();
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    if (!isSignedIn) return;
    if (attempt >= 8) {
      router.replace("/marketplace/dashboard");
      return;
    }
    const t = setTimeout(async () => {
      try {
        const jwt = await getToken();
        if (!jwt) return;
        const resp = await fetch(`${API}/api/v1/marketplace/payouts/dashboard`, {
          headers: { Authorization: `Bearer ${jwt}` },
        });
        if (resp.ok) {
          const body = (await resp.json()) as { dashboard_url?: string | null };
          if (body.dashboard_url) {
            router.replace("/marketplace/dashboard");
            return;
          }
        }
        setAttempt((a) => a + 1);
      } catch {
        setAttempt((a) => a + 1);
      }
    }, 2000);
    return () => clearTimeout(t);
  }, [attempt, isSignedIn, getToken, router]);

  return (
    <main className="max-w-md mx-auto px-6 py-16 text-center">
      <h1 className="font-editorial-serif text-2xl mb-4">Finishing setup…</h1>
      <p className="text-[var(--mute)]">
        Verifying your Stripe Connect account. This usually takes a few seconds.
      </p>
    </main>
  );
}
