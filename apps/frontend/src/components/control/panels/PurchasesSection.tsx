"use client";

// Purchase limits and this month's purchase spend per project (Link Agent Wallet). Kept apart
// from inference spend: it reads only the purchase ledger. Hidden while the feature is off
// (the API answers 404) and for anyone who cannot read it.

import { useState } from "react";
import useSWR from "swr";
import { useApi } from "@/lib/api";
import { notifyApiError } from "@/lib/notify";

interface Limits {
  perPurchaseCents: number;
  projectMonthlyCents: number;
  merchants: string[];
}
interface Summary {
  limits: Limits;
  projects: { projectId: string; name?: string | null; spentCents: number }[];
  recent: { requestId: string; merchant: string; amountCents: number; status: string }[];
}

const usd = (cents: number) => `$${(cents / 100).toFixed(2)}`;
const PATH = "/company/link-wallet/purchases";

export function PurchasesSection({ canEdit }: { canEdit: boolean }) {
  const api = useApi();
  const { data, error, mutate } = useSWR<Summary>(PATH, (p: string) => api.get(p) as Promise<Summary>, {
    shouldRetryOnError: false,
  });
  const [per, setPer] = useState("");
  const [monthly, setMonthly] = useState("");
  const [merchants, setMerchants] = useState("");
  const [busy, setBusy] = useState(false);

  if (error || !data) return null;
  const { limits } = data;
  const cap = limits.projectMonthlyCents;

  async function save() {
    const next = {
      perPurchaseCents: per.trim() ? Math.round(parseFloat(per) * 100) : limits.perPurchaseCents,
      projectMonthlyCents: monthly.trim() ? Math.round(parseFloat(monthly) * 100) : limits.projectMonthlyCents,
      merchants: merchants.trim()
        ? merchants.split(/[\s,]+/).filter(Boolean)
        : limits.merchants,
    };
    if (!Number.isFinite(next.perPurchaseCents) || !Number.isFinite(next.projectMonthlyCents)) return;
    setBusy(true);
    try {
      await api.put("/company/link-wallet/limits", next);
      setPer("");
      setMonthly("");
      setMerchants("");
      await mutate();
    } catch (e) {
      notifyApiError(e, "Could not save the purchase limits.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="flex flex-col gap-3" data-testid="purchases-section">
      <h3 className="text-sm font-medium text-[var(--fg)]">Purchases</h3>
      <p className="text-sm text-[var(--fg-soft)]">
        Agents can ask to buy things with your Link wallet. Link always asks you to approve. Lightbulb asks first
        too, unless the merchant is on your trusted list and &quot;ask before moving money&quot; is off. Until both
        limits are set, purchases are off.
      </p>
      <div className="text-xs text-[var(--fg-soft)]">
        Per purchase: {limits.perPurchaseCents ? usd(limits.perPurchaseCents) : "not set"} · Per project each
        month: {cap ? usd(cap) : "not set"} · Skip the extra approval for:{" "}
        {limits.merchants.length ? limits.merchants.join(", ") : "no merchants"}
      </div>
      {data.projects.length > 0 && (
        <ul className="rounded-lg border border-[var(--plane-edge)] bg-[var(--plane-raised)]">
          {data.projects.map((p) => (
            <li key={p.projectId} className="flex flex-col gap-1 border-b border-[var(--plane-edge)] px-4 py-3">
              <div className="flex justify-between text-sm text-[var(--fg)]">
                <span>{p.name || p.projectId}</span>
                <span>{cap ? `${usd(p.spentCents)} of ${usd(cap)}` : usd(p.spentCents)}</span>
              </div>
              {cap > 0 && (
                <div
                  role="progressbar"
                  aria-label={`Purchases for ${p.name || p.projectId}`}
                  aria-valuenow={Math.min(100, Math.round((p.spentCents / cap) * 100))}
                  aria-valuemin={0}
                  aria-valuemax={100}
                  className="h-1.5 rounded bg-[var(--plane-edge)]"
                >
                  <div
                    className="h-1.5 rounded"
                    style={{
                      width: `${Math.min(100, (p.spentCents / cap) * 100)}%`,
                      background: p.spentCents >= cap ? "var(--alert)" : "var(--brand)",
                    }}
                  />
                </div>
              )}
            </li>
          ))}
        </ul>
      )}
      {canEdit && (
        <form
          className="flex flex-wrap items-end gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            if (!busy) void save();
          }}
        >
          <label className="flex flex-col text-xs text-[var(--fg-soft)]">
            Per purchase ($)
            <input
              type="number"
              min="0"
              step="0.01"
              value={per}
              onChange={(e) => setPer(e.target.value)}
              className="w-28 rounded-md border border-[var(--plane-edge)] bg-[var(--plane-raised)] px-2 py-1 text-sm text-[var(--fg)]"
            />
          </label>
          <label className="flex flex-col text-xs text-[var(--fg-soft)]">
            Per project each month ($)
            <input
              type="number"
              min="0"
              step="0.01"
              value={monthly}
              onChange={(e) => setMonthly(e.target.value)}
              className="w-28 rounded-md border border-[var(--plane-edge)] bg-[var(--plane-raised)] px-2 py-1 text-sm text-[var(--fg)]"
            />
          </label>
          <label className="flex min-w-[200px] flex-1 flex-col text-xs text-[var(--fg-soft)]">
            Trusted merchants (comma separated)
            <input
              value={merchants}
              onChange={(e) => setMerchants(e.target.value)}
              className="rounded-md border border-[var(--plane-edge)] bg-[var(--plane-raised)] px-2 py-1 text-sm text-[var(--fg)]"
            />
          </label>
          <button
            type="submit"
            disabled={busy}
            className="rounded-md border border-[var(--plane-edge)] px-2 py-1 text-sm text-[var(--fg)] disabled:opacity-50"
          >
            Save limits
          </button>
        </form>
      )}
    </section>
  );
}
