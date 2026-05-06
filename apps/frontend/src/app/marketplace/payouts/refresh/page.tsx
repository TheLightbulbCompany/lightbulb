"use client";

import { useAuth } from "@clerk/nextjs";
import { useEffect, useState } from "react";

const API = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000/api/v1";

/**
 * Stripe Connect onboarding "refresh" landing page.
 *
 * Stripe redirects sellers here when an onboarding link expires (or they
 * cancel mid-flow and click the refresh button). We re-POST to /onboard
 * to mint a fresh hosted URL, then redirect.
 */
export default function PayoutsRefreshPage() {
  const { isSignedIn, getToken } = useAuth();
  const [status, setStatus] = useState<"refreshing" | "error">("refreshing");
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  useEffect(() => {
    if (!isSignedIn) return;

    let cancelled = false;
    (async () => {
      try {
        const jwt = await getToken();
        if (!jwt || cancelled) return;
        const resp = await fetch(`${API}/marketplace/payouts/onboard`, {
          method: "POST",
          headers: { Authorization: `Bearer ${jwt}` },
        });
        if (!resp.ok) {
          const txt = await resp.text();
          setErrorMsg(`${resp.status}: ${txt.slice(0, 200)}`);
          setStatus("error");
          return;
        }
        const body = (await resp.json()) as { onboarding_url?: string };
        if (body.onboarding_url) {
          window.location.href = body.onboarding_url;
        } else {
          setStatus("error");
          setErrorMsg("Stripe didn't return a fresh onboarding URL.");
        }
      } catch (e) {
        setStatus("error");
        setErrorMsg(e instanceof Error ? e.message : "unknown");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [isSignedIn, getToken]);

  return (
    <main className="max-w-md mx-auto px-6 py-16 text-center">
      <h1 className="font-editorial-serif text-2xl mb-4">Refreshing Stripe onboarding…</h1>
      {status === "refreshing" && (
        <p className="text-[var(--mute)]">
          Generating a fresh onboarding link, hold tight.
        </p>
      )}
      {status === "error" && (
        <div className="rounded border border-[rgba(179,36,36,0.2)] bg-[rgba(179,36,36,0.06)] p-4 text-left">
          <p className="text-[var(--danger)] font-medium">Couldn&apos;t refresh onboarding.</p>
          {errorMsg && <p className="text-[var(--mute)] text-sm mt-2">{errorMsg}</p>}
        </div>
      )}
    </main>
  );
}
