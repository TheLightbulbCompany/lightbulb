"use client";

import { useState } from "react";
import useSWR from "swr";
import { useApi } from "@/lib/api";
import { notifyApiError } from "@/lib/notify";

// Fallback deep links for providers whose `landed` post carried no dashboard
// URL. Only providers the Projects catalog actually serves (spec §3.2).
// Doubles as the ALLOWLIST for `dashboard_url` — see dashboardHref.
const PROVIDER_HOME: Record<string, string> = {
  posthog: "https://posthog.com",
  vercel: "https://vercel.com/dashboard",
  supabase: "https://supabase.com/dashboard",
  neon: "https://console.neon.tech",
  clerk: "https://dashboard.clerk.com",
  cloudflare: "https://dash.cloudflare.com",
  twilio: "https://console.twilio.com",
  resend: "https://resend.com/overview",
  sentry: "https://sentry.io",
  e2b: "https://e2b.dev/dashboard",
};

type Resource = {
  resource_id: string;
  provider: string;
  service: string;
  status: string;
  resource_name?: string | null;
  /** Variable NAMES only — the values never leave the container. */
  env_keys: string[];
  dashboard_url?: string | null;
  level: string;
};

type AccountsPayload = { resources: Resource[] };

const PATH = "/company/provisioning/resources";

/** Where "Open dashboard" may point, or null for no link at all.
 *
 * `dashboard_url` comes from an LLM-driven agent, so it is honoured only when it
 * parses, is https, and sits on the provider's OWN host (exact, or a subdomain
 * of it) — `us.posthog.com` works, an attacker-chosen host does not. Otherwise
 * the provider's known home page; and for a provider we have no home page for,
 * NO link, because a guessed `https://{provider}.com` is a host somebody else
 * owns. `Object.hasOwn` because `provider` is untrusted too. */
export function dashboardHref(provider: string, dashboardUrl?: string | null): string | null {
  const home = Object.hasOwn(PROVIDER_HOME, provider) ? PROVIDER_HOME[provider] : undefined;
  if (dashboardUrl && home) {
    try {
      const url = new URL(dashboardUrl);
      const host = new URL(home).hostname;
      if (url.protocol === "https:" && (url.hostname === host || url.hostname.endsWith(`.${host}`))) {
        return dashboardUrl;
      }
    } catch {
      // Unparseable — fall through to the provider home.
    }
  }
  return home ?? null;
}

export function CompanyAccountsSection({ canManage }: { canManage: boolean }) {
  const api = useApi();
  const { data, error, mutate, isLoading } = useSWR<AccountsPayload>(
    PATH,
    (p: string) => api.get(p) as Promise<AccountsPayload>,
  );
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);

  async function revoke(r: Resource) {
    setBusy(true);
    try {
      await api.post(`${PATH}/${r.provider}/${r.resource_id}/revoke`, {});
      setNote(
        "Disconnected from Lightbulb AI. You keep the account — remove it in the provider's own dashboard if you want it gone.",
      );
      await mutate();
    } catch (e) {
      notifyApiError(e, "Couldn't disconnect that account. Try again.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="settings-card" data-testid="company-accounts">
      <div className="settings-card-header">
        <h2 className="settings-card-title">Accounts</h2>
        <p className="settings-card-desc">
          Provider accounts your agents set up <strong>on your own Stripe identity</strong>. You own
          every one of them and keep them if you leave — Lightbulb AI only holds a copy of the
          credential so your agents can use it.
        </p>
      </div>

      {error && !data && (
        <p className="text-sm text-[var(--err)]">Couldn&apos;t load your accounts. Refresh to try again.</p>
      )}

      {!error && isLoading && <p className="text-sm text-[var(--midnight-muted)]">Loading…</p>}

      {!error && !isLoading && (
        <>
          {note && <p className="text-sm text-[var(--midnight-muted)] mb-3">{note}</p>}

          {(data?.resources.length ?? 0) === 0 ? (
            <p className="text-sm text-[var(--midnight-muted)]">
              No provider accounts yet. Ask your agents for what the business needs — analytics,
              hosting, email — and they will ask you to approve each one here.
            </p>
          ) : (
            <ul className="flex flex-col gap-2">
              {data?.resources.map((r) => {
                const href = dashboardHref(r.provider, r.dashboard_url);
                return (
                  <li key={`${r.provider}/${r.resource_id}`} className="flex flex-wrap items-center gap-3 text-sm">
                    <span className="font-medium text-[var(--midnight)]">{r.provider}</span>
                    <span className="text-[var(--midnight-muted)]">{r.service}</span>
                    {r.resource_name && <span className="text-[var(--midnight-muted)]">{r.resource_name}</span>}
                    <span className="text-[var(--midnight-muted)]">{r.status}</span>
                    {/* Spec §3.4: state the level per resource. */}
                    <span className="text-[var(--midnight-muted)]">
                      {r.level === "company" ? "Company-level" : "Agent-level"}
                    </span>
                    {href && (
                      <a className="settings-btn-outline" href={href} target="_blank" rel="noopener noreferrer">
                        Open dashboard
                      </a>
                    )}
                    {canManage && (
                      <button type="button" className="settings-btn-outline" disabled={busy} onClick={() => revoke(r)}>
                        Revoke
                      </button>
                    )}
                  </li>
                );
              })}
            </ul>
          )}
        </>
      )}
    </section>
  );
}
