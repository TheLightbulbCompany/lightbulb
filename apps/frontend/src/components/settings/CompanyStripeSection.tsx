"use client";

import { useState } from "react";
import useSWR from "swr";
import { useApi } from "@/lib/api";
import { notifyApiError } from "@/lib/notify";

type CompanyStripeStatus = {
  account_id: string | null;
  source: "created" | "connected" | null;
  details_submitted: boolean;
  charges_enabled: boolean;
  payouts_enabled: boolean;
  dashboard_url: string | null;
};

const STATUS_PATH = "/company/stripe/status";

export function CompanyStripeSection({ canManage }: { canManage: boolean }) {
  const api = useApi();
  const { data, mutate, isLoading } = useSWR<CompanyStripeStatus>(
    STATUS_PATH,
    (p: string) => api.get(p) as Promise<CompanyStripeStatus>,
  );
  const [busy, setBusy] = useState(false);

  async function run(fn: () => Promise<void>) {
    setBusy(true);
    try {
      await fn();
      await mutate();
    } catch (e) {
      notifyApiError(e, "Stripe action failed. Try again.");
    } finally {
      setBusy(false);
    }
  }

  const bound = Boolean(data?.account_id);

  // The shared client returns `unknown`; narrow at the call site.
  const createLink = async () => (await api.post("/company/stripe/create", {})) as { onboarding_url: string };
  const onboardingLink = async () =>
    (await api.post("/company/stripe/onboarding-link", {})) as { onboarding_url: string };
  const connectStart = async () => (await api.get("/company/stripe/connect/start")) as { authorize_url: string };

  return (
    <section className="settings-card" data-testid="company-stripe">
      <div className="settings-card-header">
        <h2 className="settings-card-title">Stripe</h2>
        <p className="settings-card-desc">
          Your company&apos;s own Stripe account. You keep the account, the Dashboard and 100% of your
          revenue — Lightbulb AI takes no fee.
        </p>
      </div>

      {isLoading && <p className="text-sm text-[var(--mute)]">Loading…</p>}

      {!isLoading && !bound && (
        <div className="flex flex-wrap gap-2">
          {canManage ? (
            <>
              <button
                type="button"
                className="settings-btn-save"
                disabled={busy}
                onClick={() =>
                  run(async () => {
                    const r = await createLink();
                    window.open(r.onboarding_url, "_blank", "noopener");
                  })
                }
              >
                Set up Stripe
              </button>
              <button
                type="button"
                className="settings-btn-outline"
                disabled={busy}
                onClick={() =>
                  run(async () => {
                    const r = await connectStart();
                    window.open(r.authorize_url, "_blank", "noopener");
                  })
                }
              >
                Connect existing Stripe
              </button>
            </>
          ) : (
            <p className="text-sm text-[var(--mute)]">A workspace admin sets up Stripe.</p>
          )}
        </div>
      )}

      {!isLoading && bound && data && (
        <div className="flex flex-col gap-2">
          <dl className="text-sm text-[var(--mute)]">
            <div className="flex gap-2"><dt>Account</dt><dd className="font-mono text-[#1c1917]">{data.account_id}</dd></div>
            <div className="flex gap-2"><dt>Source</dt><dd>{data.source === "connected" ? "Connected existing account" : "Created by Lightbulb AI"}</dd></div>
            <div className="flex gap-2">
              <dt>Status</dt>
              <dd>
                {data.charges_enabled ? "Charges enabled" : data.details_submitted ? "Under review" : "Onboarding incomplete"}
                {data.payouts_enabled ? " · Payouts enabled" : ""}
              </dd>
            </div>
          </dl>
          <div className="flex flex-wrap gap-2">
            {data.dashboard_url && (
              <a className="settings-btn-outline" href={data.dashboard_url} target="_blank" rel="noopener noreferrer">
                Open Stripe Dashboard
              </a>
            )}
            {canManage && data.source === "created" && !data.charges_enabled && (
              <button
                type="button"
                className="settings-btn-save"
                disabled={busy}
                onClick={() =>
                  run(async () => {
                    const r = await onboardingLink();
                    window.open(r.onboarding_url, "_blank", "noopener");
                  })
                }
              >
                Finish onboarding
              </button>
            )}
            {canManage && (
              <button
                type="button"
                className="settings-btn-outline"
                disabled={busy}
                onClick={() => run(async () => { await api.post("/company/stripe/disconnect", {}); })}
              >
                Disconnect
              </button>
            )}
          </div>
        </div>
      )}
    </section>
  );
}
