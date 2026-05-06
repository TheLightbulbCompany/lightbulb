"use client";
import { useAuth } from "@clerk/nextjs";
import { useState } from "react";
import useSWR from "swr";

const API = process.env.NEXT_PUBLIC_API_URL ?? "https://api.isol8.co";

export default function Dashboard() {
  const { getToken } = useAuth();
  const { data } = useSWR(
    `${API}/api/v1/marketplace/payouts/dashboard`,
    async (url) => {
      const jwt = await getToken();
      const resp = await fetch(url, { headers: { Authorization: `Bearer ${jwt}` } });
      if (!resp.ok) return null;
      return resp.json();
    }
  );
  const [onboardLoading, setOnboardLoading] = useState(false);
  const [onboardError, setOnboardError] = useState<string | null>(null);

  async function startOnboarding() {
    setOnboardError(null);
    setOnboardLoading(true);
    try {
      const jwt = await getToken();
      const resp = await fetch(`${API}/api/v1/marketplace/payouts/onboard`, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${jwt}`,
          "Content-Type": "application/json",
        },
      });
      if (!resp.ok) {
        setOnboardError(`onboard failed (${resp.status})`);
        return;
      }
      const body = (await resp.json()) as { onboarding_url?: string };
      if (body.onboarding_url) {
        window.location.href = body.onboarding_url;
      } else {
        setOnboardError("missing onboarding_url in response");
      }
    } catch (e) {
      setOnboardError(e instanceof Error ? e.message : "network error");
    } finally {
      setOnboardLoading(false);
    }
  }

  if (!data) return <p className="p-8">Loading…</p>;
  return (
    <main className="max-w-3xl mx-auto px-6 py-12">
      <h1 className="font-editorial-serif text-4xl mb-8">Creator dashboard</h1>
      <div className="grid grid-cols-2 gap-4 mb-8">
        <Stat label="Held balance" value={`$${((data.balance_held_cents ?? 0) / 100).toFixed(2)}`} />
        <Stat
          label="Lifetime earned"
          value={`$${((data.lifetime_earned_cents ?? 0) / 100).toFixed(2)}`}
        />
      </div>
      {data.dashboard_url ? (
        <a href={data.dashboard_url} className="text-[var(--ink)] underline">
          Open Stripe dashboard →
        </a>
      ) : (
        <div>
          <button
            type="button"
            onClick={startOnboarding}
            disabled={onboardLoading}
            className="editorial-btn editorial-btn--primary disabled:opacity-50"
          >
            {onboardLoading ? "Redirecting to Stripe…" : "Onboard for payouts"}
          </button>
          {onboardError && (
            <p className="mt-3 text-sm text-red-400" role="alert">
              {onboardError}
            </p>
          )}
        </div>
      )}
    </main>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="editorial-card p-4">
      <p className="text-sm text-[var(--mute)]">{label}</p>
      <p className="text-2xl font-bold mt-1">{value}</p>
    </div>
  );
}
