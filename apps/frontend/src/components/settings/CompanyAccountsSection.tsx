"use client";

import { useState } from "react";
import useSWR from "swr";
import { useApi } from "@/lib/api";
import { notifyApiError } from "@/lib/notify";

// Fallback deep links for providers whose `landed` post carried no dashboard
// URL. Only providers the Projects catalog actually serves (spec §3.2).
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

type AccountsPayload = { resources: Resource[]; stripe_cli: "paired" | "unpaired" };

const PATH = "/company/provisioning/resources";

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
          <p className="text-sm text-[var(--midnight-muted)] mb-3">
            {data?.stripe_cli === "paired" ? "Stripe CLI: connected" : "Stripe CLI: not connected"}
          </p>

          {note && <p className="text-sm text-[var(--midnight-muted)] mb-3">{note}</p>}

          {(data?.resources.length ?? 0) === 0 ? (
            <p className="text-sm text-[var(--midnight-muted)]">
              No provider accounts yet. Ask your agents for what the business needs — analytics,
              hosting, email — and they will ask you to approve each one here.
            </p>
          ) : (
            <ul className="flex flex-col gap-2">
              {data?.resources.map((r) => (
                <li key={`${r.provider}/${r.resource_id}`} className="flex flex-wrap items-center gap-3 text-sm">
                  <span className="font-medium text-[var(--midnight)]">{r.provider}</span>
                  <span className="text-[var(--midnight-muted)]">{r.service}</span>
                  {r.resource_name && <span className="text-[var(--midnight-muted)]">{r.resource_name}</span>}
                  <span className="text-[var(--midnight-muted)]">{r.status}</span>
                  {/* Spec §3.4: state the level per resource. */}
                  <span className="text-[var(--midnight-muted)]">
                    {r.level === "company" ? "Company-level" : "Agent-level"}
                  </span>
                  <a
                    className="settings-btn-outline"
                    href={r.dashboard_url ?? PROVIDER_HOME[r.provider] ?? `https://${r.provider}.com`}
                    target="_blank"
                    rel="noopener noreferrer"
                  >
                    Open dashboard
                  </a>
                  {canManage && (
                    <button type="button" className="settings-btn-outline" disabled={busy} onClick={() => revoke(r)}>
                      Revoke
                    </button>
                  )}
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </section>
  );
}
