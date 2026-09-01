"use client";

import { useEffect, useRef, useState } from "react";
import useSWR from "swr";
import { useApi } from "@/lib/api";
import { notifyApiError, notifyError } from "@/lib/notify";
import { closePendingTab, navigatePendingTab, openPendingTab } from "@/components/settings/openInNewTab";
import { isTauri, openExternalUrl, pollUntil } from "@/lib/externalAuth";
// NOT `settings-btn-save` / `settings-btn-outline`: those live in the
// route-scoped `app/settings/settings.css`, which never loads in the workspace
// shell. This component renders in BOTH places — /settings and the Accounts
// panel on /chat — so on /chat every control here painted as unstyled text
// (#2268). `TeamBillingCard.tsx` documents the identical trap from an earlier
// incident. EditorialButton is token-based and route-independent, so it is
// correct on both surfaces.
import { EditorialButton } from "@/components/teams/shell";

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

/**
 * The company Stripe ACCOUNT status (Connect OAuth; backend-only credential).
 * Shared between the grant block below and StripeSection's same-account
 * hint — one SWR key, so both callers dedupe to a single request.
 */
export function useCompanyStripeStatus() {
  const api = useApi();
  return useSWR<CompanyStripeStatus>(
    STATUS_PATH,
    (p: string) => api.get(p) as Promise<CompanyStripeStatus>,
  );
}

/**
 * The "Company account" grant inside the consolidated Stripe section
 * (StripeSection): the company's OWN Stripe account. Identity — revenue
 * lands here, and providers are provisioned on it. Its credential is
 * backend-only and entirely separate from the agent-tools MCP token;
 * nothing here shares or derives one from the other.
 */
export function CompanyStripeSection({ canManage }: { canManage: boolean }) {
  const api = useApi();
  const { data, error, mutate, isLoading } = useCompanyStripeStatus();
  const [busy, setBusy] = useState(false);

  // Stops the desktop grant-poll when this section unmounts — an abandoned
  // wait must not keep issuing status GETs for up to the poll timeout.
  const pollAbortRef = useRef<AbortController | null>(null);
  useEffect(() => () => pollAbortRef.current?.abort(), []);

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
    // Desktop (Tauri): WKWebView blocks window.open, so the two-phase tab
    // dance would always read as "popup blocked". The shell plugin has no
    // user-activation constraint, so fetch the URL first, open it in the
    // SYSTEM browser, then poll our own status until Stripe's side lands
    // (any field changing — account bound, details submitted, charges on).
    // A failed open throws out of run() → error toast, no poll.
    if (isTauri()) {
      void run(async () => {
        await openExternalUrl(await fetchUrl());
        const before = JSON.stringify(data ?? null);
        const ac = new AbortController();
        pollAbortRef.current = ac;
        await pollUntil(
          async () => JSON.stringify((await mutate()) ?? null) !== before,
          { signal: ac.signal },
        );
      });
      return;
    }
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
  // Both OAuth doors 409 without the platform client id, so don't offer the
  // one that only exists there, and say why the exit is unavailable.
  const connectConfigured = Boolean(data?.connect_configured);

  // The shared client returns `unknown`; narrow at the call site.
  const createLink = async () => (await api.post("/company/stripe/create", {})) as { onboarding_url: string };
  const onboardingLink = async () =>
    (await api.post("/company/stripe/onboarding-link", {})) as { onboarding_url: string };
  const connectStart = async () => (await api.get("/company/stripe/connect/start")) as { authorize_url: string };

  return (
    <div
      className="flex items-start gap-3 rounded-md border border-[var(--edge)] bg-cream p-4"
      data-testid="company-stripe"
    >
      <span
        className={`mt-1 h-2.5 w-2.5 shrink-0 rounded-full ${
          bound ? "bg-[var(--ok)]" : "bg-[var(--midnight-faint)]"
        }`}
        aria-hidden="true"
      />
      <div className="flex min-w-0 flex-1 flex-col gap-2">
        <div>
          <h3 className="text-sm font-medium text-midnight">Company account</h3>
          <p className="text-xs text-[var(--midnight-muted)]">
            {isLoading && !error ? (
              "Loading…"
            ) : bound && data ? (
              <>
                Connected as <span className="font-mono text-midnight">{data.account_id}</span>
              </>
            ) : (
              "Not connected"
            )}
          </p>
          <p className="text-xs text-[var(--midnight-muted)] mt-0.5">
            Your identity on Stripe — payments land here, and services we set up for you are created
            on this account. You keep the account, the Dashboard and 100% of your revenue —
            Lightbulb AI takes no fee.
          </p>
        </div>

        {error && !data && (
          <p className="text-sm text-[var(--err)]">Couldn&apos;t load Stripe status. Refresh to try again.</p>
        )}

        {!error && !isLoading && !bound && (
          <div className="flex flex-wrap gap-2">
            {canManage ? (
              <>
                <EditorialButton
                  type="button"
                  size="sm"
                  disabled={busy}
                  onClick={() => openWith(async () => (await createLink()).onboarding_url)}
                >
                  Set up Stripe
                </EditorialButton>
                {connectConfigured && (
                  <EditorialButton
                    type="button"
                    size="sm"
                    variant="secondary"
                    disabled={busy}
                    onClick={() => openWith(async () => (await connectStart()).authorize_url)}
                  >
                    Connect existing Stripe
                  </EditorialButton>
                )}
              </>
            ) : (
              <p className="text-sm text-[var(--midnight-muted)]">A workspace admin sets up Stripe.</p>
            )}
          </div>
        )}

        {!isLoading && bound && data && (
          <div className="flex flex-col gap-2">
            <dl className="text-sm text-[var(--midnight-muted)]">
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
              {/* EditorialButton is button-only, so this anchor carries the same
                  global `editorial-btn` classes directly rather than a
                  route-scoped one. */}
              {data.dashboard_url && (
                <a
                  className="editorial-btn editorial-btn--secondary editorial-btn--sm"
                  href={data.dashboard_url}
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  Open Stripe Dashboard
                </a>
              )}
              {canManage && data.source === "created" && !data.charges_enabled && (
                <EditorialButton
                  type="button"
                  size="sm"
                  disabled={busy}
                  onClick={() => openWith(async () => (await onboardingLink()).onboarding_url)}
                >
                  Finish onboarding
                </EditorialButton>
              )}
              {canManage && (
                <EditorialButton
                  type="button"
                  size="sm"
                  variant="secondary"
                  disabled={busy || !connectConfigured}
                  title={connectConfigured ? undefined : "Not available until Stripe Connect is configured"}
                  onClick={() => run(async () => { await api.post("/company/stripe/disconnect", {}); })}
                >
                  Disconnect
                </EditorialButton>
              )}
            </div>
          </div>
        )}

        {busy && isTauri() && (
          <p className="text-sm text-[var(--midnight-muted)]" role="status">
            Finish in your browser — this page updates automatically.
          </p>
        )}
      </div>
    </div>
  );
}
