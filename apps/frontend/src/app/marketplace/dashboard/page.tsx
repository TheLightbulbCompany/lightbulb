"use client";
import { useState } from "react";
import useSWR from "swr";
import { useApi } from "@/lib/api";

interface DashboardResponse {
  balance_held_cents?: number;
  lifetime_earned_cents?: number;
  dashboard_url?: string;
}

export default function Dashboard() {
  const api = useApi();
  const { data, error } = useSWR<DashboardResponse>(
    "/marketplace/payouts/dashboard",
    (path: string) => api.get(path) as Promise<DashboardResponse>,
  );
  const [onboardLoading, setOnboardLoading] = useState(false);
  const [onboardError, setOnboardError] = useState<string | null>(null);

  async function startOnboarding() {
    setOnboardError(null);
    setOnboardLoading(true);
    try {
      const body = (await api.post("/marketplace/payouts/onboard", {})) as {
        onboarding_url?: string;
      };
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

  if (error) return <p className="p-8 text-[var(--danger)]">Couldn&apos;t load dashboard: {error.message}</p>;
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
            <p className="mt-3 text-sm text-[var(--danger)]" role="alert">
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
