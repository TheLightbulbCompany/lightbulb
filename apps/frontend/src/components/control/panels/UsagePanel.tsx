"use client";

import { useCallback, useState } from "react";
import useSWR from "swr";
import Link from "next/link";
import { Loader2, RefreshCw, AlertCircle } from "lucide-react";
import { useIsWorkspaceAdmin } from "@/hooks/useIsWorkspaceAdmin";
import { useBilling } from "@/hooks/useBilling";
import type { UsageSummary } from "@/hooks/useBilling";
import { useSpend } from "@/hooks/useSpend";
import { Button } from "@/components/ui/button";
import { planLabel, isFreeTierExpired } from "@/lib/planLabel";

// =============================================================================
// Helpers
// =============================================================================

function formatTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
  return n.toLocaleString();
}

function formatCents(cents: number): string {
  return `$${(cents / 100).toFixed(2)}`;
}

// =============================================================================
// Component
// =============================================================================

export interface UsagePanelProps {
  /** Rendered inside another panel's page (Money → Spend, `SpendPanel.tsx`),
   *  which already supplies the page padding and a `PanelMasthead`. Drops this
   *  component's own page padding and its "Usage" heading so the two don't
   *  stack — everything below the heading is identical in both hosts. Two
   *  registrations (`?panel=usage` and `?panel=teams-spend`), one component. */
  embedded?: boolean;
}

export function UsagePanel({ embedded = false }: UsagePanelProps = {}) {
  const { account, isLoading: accountLoading, fetchUsage, refresh } = useBilling();
  const isOrgAdmin = useIsWorkspaceAdmin();
  const { spend } = useSpend();
  // Captured once (not read directly in render — Date.now() is impure) so
  // this is stable across re-renders. Docked next to WorkspaceSidebar's
  // footer badge in the same dockable shell (WorkspaceShell.tsx), so it must
  // agree on expired — a user with Usage open next to chat must not see
  // "Free tier" here while the sidebar footer says "Free tier ended".
  const [nowMs] = useState(() => Date.now());
  const isTrialExpired = isFreeTierExpired(account?.subscription_status ?? null, account?.trial_end, nowMs);

  // SWR-cached rather than fetched into local state on mount: panels
  // unmount/remount on every shell navigation, and an uncached read here
  // meant a spinner on every open. The cache serves the last summary
  // instantly and revalidates in the background.
  const {
    data: usage,
    error: usageError,
    isLoading: usageLoading,
    mutate: mutateUsage,
  } = useSWR<UsageSummary | null>("/billing/usage", () => fetchUsage(), {
    dedupingInterval: 30000,
  });
  const error = usageError
    ? usageError instanceof Error
      ? usageError.message
      : "Failed to fetch usage data"
    : null;

  const handleRefresh = useCallback(() => {
    refresh();
    void mutateUsage();
  }, [refresh, mutateUsage]);

  if (accountLoading && usageLoading) {
    return (
      <div className="flex-1 flex items-center justify-center">
        <Loader2 size={20} className="animate-spin text-[var(--midnight-muted)]" />
      </div>
    );
  }

  return (
    <div className={embedded ? "space-y-6" : "p-6 space-y-6"}>
      <div className={embedded ? "flex justify-end" : "flex items-center justify-between"}>
        {!embedded && <h2 className="font-editorial-serif text-title">Usage</h2>}
        <Button variant="ghost" size="sm" onClick={handleRefresh} aria-label="Refresh usage">
          <RefreshCw size={16} />
        </Button>
      </div>

      {error && (
        <div className="flex items-center gap-2 px-3 py-2 rounded-lg bg-cream border border-err">
          <AlertCircle size={16} className="text-err shrink-0" />
          <span className="text-xs text-err">{error}</span>
          <Button variant="outline" size="sm" className="ml-auto h-6 text-xs" onClick={handleRefresh}>
            Retry
          </Button>
        </div>
      )}

      {account && (
        <div className="flex items-center gap-3 text-xs text-[var(--midnight-muted)]">
          {planLabel(account.subscription_status, { expired: isTrialExpired }) && (
            <span>
              Subscription:{" "}
              {/* `capitalize` would turn "Free tier" into "Free Tier" —
                  drop it for trial_local, same technique settings/page.tsx
                  uses for its billing chip. */}
              <span
                className={
                  account.subscription_status === "trial_local"
                    ? "font-medium text-[var(--midnight)]"
                    : "font-medium text-[var(--midnight)] capitalize"
                }
              >
                {planLabel(account.subscription_status, { expired: isTrialExpired })}
              </span>
            </span>
          )}
          {usage?.period && (
            <span>
              Period: <span className="font-medium text-[var(--midnight)]">{usage.period}</span>
            </span>
          )}
        </div>
      )}

      {spend?.mode === "bedrock" && (
        <div className="rounded-lg border border-[var(--edge)] p-4 space-y-3">
          <div className="flex items-center justify-between">
            <h3 className="text-sm font-medium">Billed this month</h3>
            <span className="font-mono text-title">
              {formatCents(spend.billed_month_to_date_cents ?? 0)}
            </span>
          </div>
          {(spend.by_model ?? []).map((row) => (
            <div key={row.tier} className="flex items-center justify-between text-xs text-[var(--midnight-muted)]">
              <span>{row.model}</span>
              <span className="font-mono">{formatCents(row.cents)}</span>
            </div>
          ))}
          <p className="text-2xs text-midnight-muted">
            From your credits invoice — matches what you&apos;re charged.
          </p>
          <Link
            href="/chat?panel=llm"
            className="inline-block text-xs text-[var(--midnight-muted)] hover:text-[var(--midnight)] underline underline-offset-2"
          >
            Manage credits
          </Link>
        </div>
      )}
      {spend?.mode === "oauth_quota" && (
        <div className="rounded-lg border border-[var(--edge)] p-4 space-y-3">
          <div className="flex items-center justify-between">
            <h3 className="text-sm font-medium">ChatGPT plan limits</h3>
            {spend.plan && <span className="text-xs text-[var(--midnight-muted)]">{spend.plan}</span>}
          </div>
          {(spend.windows ?? []).map((w) => (
            <div key={w.label} className="space-y-1">
              <div className="flex items-center justify-between text-xs">
                <span>{w.label} limit</span>
                <span className="text-[var(--midnight-muted)]">
                  {w.used_percent > 0 && w.used_percent < 1 ? "<1" : Math.round(w.used_percent)}% used
                  {w.resets_at_ms
                    ? ` · resets ${new Date(w.resets_at_ms).toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" })}`
                    : ""}
                </span>
              </div>
              <div className="h-1.5 rounded-sm bg-[var(--edge)]">
                <div
                  className="h-1.5 rounded-sm bg-[var(--midnight)]"
                  style={{ width: `${Math.min(100, Math.max(0, w.used_percent))}%` }}
                />
              </div>
            </div>
          ))}
          <p className="text-2xs text-midnight-muted">Included in your ChatGPT plan.</p>
          <p className="text-2xs text-midnight-muted">
            Rolling plan windows — separate from the monthly token totals below.
          </p>
        </div>
      )}

      {usage && (
        <div className="rounded-lg border border-[var(--edge)] p-4 space-y-3">
          <h3 className="text-sm font-medium">Token Usage</h3>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <div className="label-caps">Input</div>
              <div className="text-title tabular-nums">{formatTokens(usage.total_input_tokens)}</div>
            </div>
            <div>
              <div className="label-caps">Output</div>
              <div className="text-title tabular-nums">{formatTokens(usage.total_output_tokens)}</div>
            </div>
            <div>
              <div className="label-caps">Cache Read</div>
              <div className="text-title tabular-nums">{formatTokens(usage.total_cache_read_tokens)}</div>
            </div>
            <div>
              <div className="label-caps">Cache Write</div>
              <div className="text-title tabular-nums">{formatTokens(usage.total_cache_write_tokens)}</div>
            </div>
          </div>
          <div className="border-t border-[var(--edge)] pt-2 flex items-center justify-between text-xs text-[var(--midnight-muted)]">
            <span>{usage.request_count} requests this period</span>
          </div>
        </div>
      )}

      {isOrgAdmin && usage && usage.by_member && usage.by_member.length > 0 && (
        <div className="rounded-lg border border-[var(--edge)] overflow-hidden">
          <div className="px-4 py-2 bg-[var(--cream-deep)] border-b border-[var(--edge)]">
            <h3 className="text-base font-semibold">Usage by Member</h3>
          </div>
          <table className="w-full text-ui">
            <thead>
              <tr className="border-b border-[var(--edge)] text-[var(--midnight-muted)]">
                <th className="text-left px-4 py-2 label-caps">Member</th>
                <th className="text-right px-4 py-2 label-caps">Requests</th>
              </tr>
            </thead>
            <tbody>
              {usage.by_member.map((member) => (
                <tr key={member.user_id} className="border-b border-[var(--edge-soft)] hover:bg-[var(--cream-deep)]">
                  <td className="px-4 py-2">
                    <div>{member.display_name || member.email || member.user_id}</div>
                    {member.display_name && member.email && (
                      <div className="text-midnight-muted">{member.email}</div>
                    )}
                  </td>
                  <td className="px-4 py-2 text-right text-[var(--midnight-muted)]">
                    {member.request_count}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
