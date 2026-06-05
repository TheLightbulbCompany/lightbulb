"use client";

import { Loader2, RefreshCw, Radio } from "lucide-react";
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
  if (variant === "yes") return "text-[#2d8a4e] font-medium";
  if (variant === "no") return "text-[#8a8578]";
  if (variant === "error") return "text-red-600";
  if (variant === "muted") return "text-[#8a8578]/60";
  return "text-[#1a1a1a]";
}

function AccountCard({ account }: { account: ChannelAccountSnapshot }) {
  return (
    <div className="rounded-lg border border-[#e0dbd0] bg-white p-3 space-y-2">
      <div className="flex items-center justify-between">
        <span className="text-sm font-medium text-[#1a1a1a]">
          {account.name || account.accountId}
        </span>
        <span className="text-[10px] uppercase tracking-wider text-[#8a8578]/60">
          {account.accountId}
        </span>
      </div>
      <div className="grid grid-cols-2 gap-x-4 gap-y-1">
        {EXTENDED_STATUS_FIELDS.map((field) => {
          const { text, variant } = formatValue(field, account[field]);
          return (
            <div key={field} className="flex items-center justify-between text-xs">
              <span className="text-[#8a8578]">{STATUS_LABELS[field] ?? field}</span>
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
  const { data, isLoading, mutate } = useGatewayRpc<ChannelsStatusSnapshot>(
    "channels.status",
    { probe: true },
  );

  const channelOrder = data?.channelOrder ?? [];

  return (
    <div className="p-6 space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-lg font-semibold">Channels</h2>
          <p className="text-xs text-[#8a8578]">
            Live connectivity for each linked messaging channel.
          </p>
        </div>
        <Button
          variant="outline"
          size="sm"
          onClick={() => mutate()}
          className="text-xs gap-1.5"
        >
          <RefreshCw className="h-3.5 w-3.5" />
          Probe now
        </Button>
      </div>

      {isLoading && !data ? (
        <div className="flex items-center gap-2 text-sm text-[#8a8578]">
          <Loader2 className="h-4 w-4 animate-spin" /> Probing channels…
        </div>
      ) : channelOrder.length === 0 ? (
        <div className="rounded-lg border border-[#e0dbd0] bg-white p-6 text-center">
          <Radio className="h-5 w-5 mx-auto mb-2 text-[#8a8578]/50" />
          <p className="text-sm text-[#8a8578]">
            No channels configured. Link a channel from Settings to see status here.
          </p>
        </div>
      ) : (
        <div className="space-y-5">
          {channelOrder.map((channelId) => {
            const accounts = data?.channelAccounts?.[channelId] ?? [];
            return (
              <div key={channelId} className="space-y-2">
                <h3 className="text-xs font-semibold uppercase tracking-wider text-[#8a8578]">
                  {data?.channelLabels?.[channelId] ?? channelId}
                </h3>
                {accounts.length === 0 ? (
                  <p className="text-xs text-[#8a8578]/70 pl-1">No accounts linked.</p>
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
