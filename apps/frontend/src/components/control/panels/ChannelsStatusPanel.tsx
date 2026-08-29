"use client";

import { Loader2, RefreshCw, Radio, AlertTriangle } from "lucide-react";
import { useGatewayRpc } from "@/hooks/useGatewayRpc";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import {
  type ChannelsStatusSnapshot,
  type ChannelAccountSnapshot,
  EXTENDED_STATUS_FIELDS,
  STATUS_LABELS,
  formatValue,
} from "./channels-types";

// Color the value text by formatValue's variant so a "No"/error reads as a
// warning at a glance — the snapshot has no severity of its own.
function valueClass(variant: ReturnType<typeof formatValue>["variant"]): string {
  if (variant === "yes") return "text-ok font-medium";
  if (variant === "no") return "text-[var(--midnight-muted)]";
  if (variant === "error") return "text-err";
  if (variant === "muted") return "text-midnight-muted";
  return "text-[var(--midnight)]";
}

function AccountCard({ account }: { account: ChannelAccountSnapshot }) {
  return (
    <div className="rounded-lg border border-[var(--edge)] bg-cream p-3 space-y-2">
      <div className="flex items-center justify-between">
        <span className="text-sm font-medium text-[var(--midnight)]">
          {account.name || account.accountId}
        </span>
        <span className="label-caps">
          {account.accountId}
        </span>
      </div>
      <div className="grid grid-cols-2 gap-x-4 gap-y-1">
        {EXTENDED_STATUS_FIELDS.map((field) => {
          const { text, variant } = formatValue(field, account[field]);
          return (
            <div key={field} className="flex items-center justify-between text-xs">
              <span className="text-[var(--midnight-muted)]">{STATUS_LABELS[field] ?? field}</span>
              <span className={cn("truncate max-w-[60%] text-right", valueClass(variant))}>
                {text}
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

export function ChannelsStatusPanel() {
  // probe:true forces a live connectivity probe on the container rather than
  // returning the cached snapshot — the panel exists to show fresh health.
  const { data, error, isLoading, mutate } = useGatewayRpc<ChannelsStatusSnapshot>(
    "channels.status",
    { probe: true },
  );

  const channelOrder = data?.channelOrder ?? [];

  return (
    <div className="p-6 space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="font-editorial-serif text-title">Channels</h2>
          <p className="text-xs text-[var(--midnight-muted)]">
            Live connectivity for each linked messaging channel.
          </p>
        </div>
        <Button
          variant="outline"
          size="sm"
          onClick={() => mutate()}
          className="text-xs gap-1.5"
        >
          <RefreshCw size={16} />
          Probe now
        </Button>
      </div>

      {isLoading && !data ? (
        <div className="flex items-center gap-2 text-sm text-[var(--midnight-muted)]">
          <Loader2 size={16} className="animate-spin" /> Probing channels…
        </div>
      ) : error || !data ? (
        // Undefined data means the container was unreachable — a different (and
        // more actionable) diagnosis than an empty-but-reachable snapshot. We
        // must NOT collapse it into "No channels configured", which is the exact
        // failure this panel exists to investigate.
        <div
          data-testid="channels-status-unavailable"
          className="rounded-lg border border-[var(--edge)] bg-cream p-6 text-center"
        >
          <AlertTriangle size={20} className="mx-auto mb-2 text-warn" />
          <p className="text-sm text-[var(--midnight-muted)]">
            Couldn&apos;t reach the container to probe channel status.
          </p>
          <Button
            variant="outline"
            size="sm"
            onClick={() => mutate()}
            className="mt-3 text-xs gap-1.5"
          >
            <RefreshCw size={16} />
            Try again
          </Button>
        </div>
      ) : channelOrder.length === 0 ? (
        <div className="rounded-lg border border-[var(--edge)] bg-cream p-6 text-center">
          <Radio size={20} className="mx-auto mb-2 text-midnight-muted" />
          <p className="text-sm text-[var(--midnight-muted)]">
            No channels configured. Link a channel from Settings to see status here.
          </p>
        </div>
      ) : (
        <div className="space-y-5">
          {channelOrder.map((channelId) => {
            const accounts = data?.channelAccounts?.[channelId] ?? [];
            return (
              <div key={channelId} className="space-y-2">
                <h3 className="label-caps text-[var(--midnight-muted)]">
                  {data?.channelLabels?.[channelId] ?? channelId}
                </h3>
                {accounts.length === 0 ? (
                  <p className="text-xs text-midnight-muted pl-1">No accounts linked.</p>
                ) : (
                  <div className="space-y-2">
                    {accounts.map((account) => (
                      <AccountCard key={account.accountId} account={account} />
                    ))}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
