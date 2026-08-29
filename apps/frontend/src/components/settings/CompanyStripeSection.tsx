"use client";

import { useState } from "react";
import useSWR from "swr";
import { useApi } from "@/lib/api";
import { notifyApiError, notifyError } from "@/lib/notify";
import { closePendingTab, navigatePendingTab, openPendingTab } from "@/components/settings/openInNewTab";

const POPUP_BLOCKED_MSG =
  "Your browser blocked the Stripe window — allow popups for this site and try again.";

type CompanyStripeStatus = {
  account_id: string | null;
  source: "created" | "connected" | null;
  details_submitted: boolean;
  charges_enabled: boolean;
  payouts_enabled: boolean;
  dashboard_url: string | null;
  connect_configured: boolean;
};

const STATUS_PATH = "/company/stripe/status";

export function CompanyStripeSection({ canManage }: { canManage: boolean }) {
  const api = useApi();
  const { data, error, mutate, isLoading } = useSWR<CompanyStripeStatus>(
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

  /**
   * Open the tab SYNCHRONOUSLY (the click is the only moment the browser
   * lets us), then point it at the URL the API mints. Anything after the
   * first `await` is outside the user-activation window and Safari refuses.
   */
  function openWith(fetchUrl: () => Promise<string>) {
    const w = openPendingTab();
    if (!w) {
      notifyError(POPUP_BLOCKED_MSG);
      return;
    }
    void run(async () => {
      let url: string;
      try {
        url = await fetchUrl();
      } catch (e) {
        closePendingTab(w);
        throw e;
      }
      navigatePendingTab(w, url);
    });
  }

  const bound = Boolean(data?.account_id);
  // Both OAuth doors 503 without the platform client id, so don't offer the
  // one that only exists there, and say why the exit is unavailable.
  const connectConfigured = Boolean(data?.connect_configured);

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

      {error && !data && (
        <p className="text-sm text-[var(--err)]">Couldn&apos;t load Stripe status. Refresh to try again.</p>
      )}

      {!error && isLoading && <p className="text-sm text-[var(--mute)]">Loading…</p>}

      {!error && !isLoading && !bound && (
        <div className="flex flex-wrap gap-2">
          {canManage ? (
            <>
              <button
                type="button"
                className="settings-btn-save"
                disabled={busy}
                onClick={() => openWith(async () => (await createLink()).onboarding_url)}
              >
                Set up Stripe
              </button>
              {connectConfigured && (
                <button
                  type="button"
                  className="settings-btn-outline"
                  disabled={busy}
                  onClick={() => openWith(async () => (await connectStart()).authorize_url)}
                >
                  Connect existing Stripe
                </button>
              )}
            </>
          ) : (
            <p className="text-sm text-[var(--mute)]">A workspace admin sets up Stripe.</p>
          )}
        </div>
      )}

      {!isLoading && bound && data && (
        <div className="flex flex-col gap-2">
          <dl className="text-sm text-[var(--mute)]">
            <div className="flex gap-2"><dt>Account</dt><dd className="font-mono text-[var(--midnight)]">{data.account_id}</dd></div>
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
                onClick={() => openWith(async () => (await onboardingLink()).onboarding_url)}
              >
                Finish onboarding
              </button>
            )}
            {canManage && (
              <button
                type="button"
                className="settings-btn-outline"
                disabled={busy || !connectConfigured}
                title={connectConfigured ? undefined : "Not available until Stripe Connect is configured"}
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
