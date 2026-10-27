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
import { Input } from "@/components/ui/input";

const POPUP_BLOCKED_MSG =
  "Your browser blocked the Stripe window — allow popups for this site and try again.";

export type CompanyStripeStatus = {
  account_id: string | null;
  source: "created" | "connected" | "api_key" | null;
  details_submitted: boolean;
  charges_enabled: boolean;
  payouts_enabled: boolean;
  /** false = bound with a test-mode key (sandbox install / Stripe review). */
  livemode?: boolean | null;
  dashboard_url: string | null;
  connect_configured: boolean;
  /** Connect is only offered while the provisioning rail is live — it is the
   *  one thing an OAuth grant does that a pasted key cannot. */
  provisioning_enabled: boolean;
  /** false = the account exists but Stripe has not activated its `projects`
   *  capability yet (the owner owes onboarding); null = not asked. */
  projects_ready?: boolean | null;
};

/** The six read permissions the Money surface needs — shown so nobody has to
 *  guess at ~80 checkboxes. Confirmed against the permissions reference. */
const REQUIRED_PERMISSIONS = "Charges, Customers, Products, Prices, Balance, Account — all Read";

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
  const [pasting, setPasting] = useState(false);
  const [keyDraft, setKeyDraft] = useState("");

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
  // The Connect doors exist for provisioning. While that rail is off they
  // would ask an owner for a read_write grant to power a read-only tab a
  // pasted key already powers — so they are not offered.
  const connectOffered = Boolean(data?.provisioning_enabled) && connectConfigured;
  const isApiKey = data?.source === "api_key";
  // Disconnect needs the client id only for Connect rows; an api_key row has
  // nothing at Stripe to revoke.
  const canDisconnect = isApiKey || connectConfigured;

  async function submitKey() {
    const key = keyDraft.trim();
    if (!key) return;
    await run(async () => {
      await api.post("/company/stripe/api-key", { api_key: key });
      setKeyDraft("");
      setPasting(false);
    });
  }

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
          <div className="flex flex-col gap-2">
            {canManage ? (
              pasting ? (
                <form
                  className="flex flex-col gap-2"
                  onSubmit={(e) => {
                    e.preventDefault();
                    void submitKey();
                  }}
                >
                  <label className="text-xs text-[var(--midnight-muted)]" htmlFor="company-stripe-key">
                    Paste a restricted key from your Stripe Dashboard → Developers → API keys →
                    Create restricted key. Permissions: {REQUIRED_PERMISSIONS}.
                  </label>
                  <Input
                    id="company-stripe-key"
                    type="password"
                    autoComplete="off"
                    spellCheck={false}
                    placeholder="rk_live_…"
                    value={keyDraft}
                    disabled={busy}
                    onChange={(e) => setKeyDraft(e.target.value)}
                    className="h-9 bg-cream font-mono"
                  />
                  <div className="flex flex-wrap gap-2">
                    <EditorialButton type="submit" size="sm" disabled={busy || !keyDraft.trim()}>
                      Connect
                    </EditorialButton>
                    <EditorialButton
                      type="button"
                      size="sm"
                      variant="secondary"
                      disabled={busy}
                      onClick={() => {
                        setPasting(false);
                        setKeyDraft("");
                      }}
                    >
                      Cancel
                    </EditorialButton>
                  </div>
                </form>
              ) : (
                <div className="flex flex-wrap gap-2">
                  <EditorialButton type="button" size="sm" disabled={busy} onClick={() => setPasting(true)}>
                    Add API key
                  </EditorialButton>
                  {connectOffered && (
                    <>
                      <EditorialButton
                        type="button"
                        size="sm"
                        variant="secondary"
                        disabled={busy}
                        onClick={() => openWith(async () => (await createLink()).onboarding_url)}
                      >
                        Set up Stripe
                      </EditorialButton>
                      <EditorialButton
                        type="button"
                        size="sm"
                        variant="secondary"
                        disabled={busy}
                        onClick={() => openWith(async () => (await connectStart()).authorize_url)}
                      >
                        Connect existing Stripe
                      </EditorialButton>
                    </>
                  )}
                </div>
              )
            ) : (
              <p className="text-sm text-[var(--midnight-muted)]">A workspace admin sets up Stripe.</p>
            )}
          </div>
        )}

        {!isLoading && bound && data && (
          <div className="flex flex-col gap-2">
            <dl className="text-sm text-[var(--midnight-muted)]">
              <div className="flex gap-2">
                <dt>Source</dt>
                <dd>
                  {data.source === "api_key"
                    ? data.livemode === false
                      ? "Your API key · Test mode"
                      : "Your API key"
                    : data.source === "connected"
                      ? "Connected via Stripe"
                      : "Created by Lightbulb AI"}
                </dd>
              </div>
              <div className="flex gap-2">
                <dt>Status</dt>
                <dd>
                  {data.charges_enabled ? "Charges enabled" : data.details_submitted ? "Under review" : "Onboarding incomplete"}
                  {data.payouts_enabled ? " · Payouts enabled" : ""}
                </dd>
              </div>
            </dl>
            {data.projects_ready === false && data.source !== "created" && (
              <p className="text-sm text-[var(--midnight-soft)]">
                {data.source === "api_key"
                  ? "Setting up services needs a Stripe account Lightbulb created or connected — a pasted API key can't enable it."
                  : "Stripe hasn't enabled Projects on this account. Finish setup in your Stripe Dashboard."}
              </p>
            )}
            {data.source === "connected" && !data.details_submitted && (
              // "Onboarding incomplete" names the problem; this names the fix.
              // A connected account is finished in Stripe's own Dashboard —
              // there is no in-app button for it, unlike accounts we created —
              // and the Money tabs send owners here with exactly that promise.
              <p className="text-sm text-[var(--midnight-soft)]">
                Finish onboarding in your Stripe Dashboard — open it below. Payments unlock once
                Stripe enables charges.
              </p>
            )}
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
              {canManage && data.source === "created" && (!data.charges_enabled || data.projects_ready === false) && (
                <EditorialButton
                  type="button"
                  size="sm"
                  disabled={busy}
                  onClick={() => openWith(async () => (await onboardingLink()).onboarding_url)}
                >
                  {data.projects_ready === false ? "Finish Stripe setup" : "Finish onboarding"}
                </EditorialButton>
              )}
              {canManage && (
                <EditorialButton
                  type="button"
                  size="sm"
                  variant="secondary"
                  disabled={busy || !canDisconnect}
                  title={canDisconnect ? undefined : "Not available until Stripe Connect is configured"}
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
