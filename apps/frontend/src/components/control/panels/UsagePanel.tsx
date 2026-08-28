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

export function UsagePanel() {
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
        <Loader2 className="h-5 w-5 animate-spin text-[var(--midnight-muted)]" />
      </div>
    );
  }

  return (
    <div className="p-6 space-y-6">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold">Usage</h2>
        <Button variant="ghost" size="sm" onClick={handleRefresh}>
          <RefreshCw className="h-3.5 w-3.5" />
        </Button>
      </div>

      {error && (
        <div className="flex items-center gap-2 px-3 py-2 rounded-md bg-red-50 border border-red-200">
          <AlertCircle className="h-3.5 w-3.5 text-red-600 flex-shrink-0" />
          <span className="text-xs text-red-600">{error}</span>
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
            <span className="text-lg font-semibold font-mono">
              {formatCents(spend.billed_month_to_date_cents ?? 0)}
            </span>
          </div>
          {(spend.by_model ?? []).map((row) => (
            <div key={row.tier} className="flex items-center justify-between text-xs text-[var(--midnight-muted)]">
              <span>{row.model}</span>
              <span className="font-mono">{formatCents(row.cents)}</span>
            </div>
          ))}
          <p className="text-[10px] text-[var(--midnight-muted)]/60">
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
              <div className="h-1.5 rounded bg-[var(--edge)]">
                <div
                  className="h-1.5 rounded bg-[var(--midnight)]"
                  style={{ width: `${Math.min(100, Math.max(0, w.used_percent))}%` }}
                />
              </div>
            </div>
          ))}
          <p className="text-[10px] text-[var(--midnight-muted)]/60">Included in your ChatGPT plan.</p>
          <p className="text-[10px] text-[var(--midnight-muted)]/60">
            Rolling plan windows — separate from the monthly token totals below.
          </p>
        </div>
      )}

      {usage && (
        <div className="rounded-lg border border-[var(--edge)] p-4 space-y-3">
          <h3 className="text-sm font-medium">Token Usage</h3>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <div className="text-[10px] uppercase tracking-wider text-[var(--midnight-muted)]/60">Input</div>
              <div className="text-lg font-semibold">{formatTokens(usage.total_input_tokens)}</div>
            </div>
            <div>
              <div className="text-[10px] uppercase tracking-wider text-[var(--midnight-muted)]/60">Output</div>
              <div className="text-lg font-semibold">{formatTokens(usage.total_output_tokens)}</div>
            </div>
            <div>
              <div className="text-[10px] uppercase tracking-wider text-[var(--midnight-muted)]/60">Cache Read</div>
              <div className="text-lg font-semibold">{formatTokens(usage.total_cache_read_tokens)}</div>
            </div>
            <div>
              <div className="text-[10px] uppercase tracking-wider text-[var(--midnight-muted)]/60">Cache Write</div>
              <div className="text-lg font-semibold">{formatTokens(usage.total_cache_write_tokens)}</div>
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
            <h3 className="text-sm font-medium">Usage by Member</h3>
          </div>
          <table className="w-full text-xs">
            <thead>
              <tr className="border-b border-[var(--edge)] text-[var(--midnight-muted)]">
                <th className="text-left px-4 py-2 font-medium">Member</th>
                <th className="text-right px-4 py-2 font-medium">Requests</th>
              </tr>
            </thead>
            <tbody>
              {usage.by_member.map((member) => (
                <tr key={member.user_id} className="border-b border-[var(--edge)]/50 hover:bg-[var(--cream-deep)]/50">
                  <td className="px-4 py-2">
                    <div>{member.display_name || member.email || member.user_id}</div>
                    {member.display_name && member.email && (
                      <div className="text-[var(--midnight-muted)]/60">{member.email}</div>
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
