"use client";

import { useState } from "react";
import {
  Loader2,
  RefreshCw,
  Radio,
  AlertTriangle,
  ShieldCheck,
  UserRound,
} from "lucide-react";
import { useGatewayRpc } from "@/hooks/useGatewayRpc";
import {
  type ChannelPairingRequest,
  useChannelPairingRequests,
} from "@/hooks/useChannelPairingRequests";
import { Button } from "@/components/ui/button";
import { useApi } from "@/lib/api";
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
function valueClass(
  variant: ReturnType<typeof formatValue>["variant"],
): string {
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
        <span className="label-caps">{account.accountId}</span>
      </div>
      <div className="grid grid-cols-2 gap-x-4 gap-y-1">
        {EXTENDED_STATUS_FIELDS.map((field) => {
          const { text, variant } = formatValue(field, account[field]);
          return (
            <div
              key={field}
              className="flex items-center justify-between text-xs"
            >
              <span className="text-[var(--midnight-muted)]">
                {STATUS_LABELS[field] ?? field}
              </span>
              <span
                className={cn(
                  "truncate max-w-[60%] text-right",
                  valueClass(variant),
                )}
              >
                {text}
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function PairingRequestCard({
  pairingRequest,
  onResolved,
}: {
  pairingRequest: ChannelPairingRequest;
  onResolved: () => Promise<unknown>;
}) {
  const api = useApi();
  const [busy, setBusy] = useState<"approve" | "reject" | null>(null);
  const [error, setError] = useState<string | null>(null);

  const decide = async (decision: "approve" | "reject") => {
    setBusy(decision);
    setError(null);
    try {
      await api.post(`/channels/pairing/${decision}`, {
        channel: pairingRequest.channel,
        account_id: pairingRequest.accountId,
        request_id: pairingRequest.requestId,
      });
    } catch (caught) {
      setError(
        caught instanceof Error
          ? caught.message
          : `Could not ${decision} this request.`,
      );
      setBusy(null);
      return;
    }

    // The decision already succeeded. A transient revalidation failure must not
    // invite an unsafe duplicate approve/reject; SWR will poll again shortly.
    // Keep the controls disabled until this refresh settles so a fast second
    // click cannot submit the same immutable request ID twice.
    try {
      await onResolved();
    } catch {
      // Keep the successful action result distinct from list refresh state.
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="rounded-lg border border-[var(--edge)] bg-cream p-3 space-y-3">
      <div className="flex items-start gap-3">
        <UserRound
          size={18}
          className="mt-0.5 shrink-0 text-[var(--midnight-muted)]"
        />
        <div className="min-w-0 flex-1">
          <p className="text-sm font-medium text-[var(--midnight)] truncate">
            {pairingRequest.senderLabel}
          </p>
          <p className="text-xs text-[var(--midnight-muted)] font-mono break-all">
            {pairingRequest.senderId}
          </p>
          <p className="mt-1 text-xs text-[var(--midnight-muted)]">
            {pairingRequest.channelLabel} ·{" "}
            {pairingRequest.accountLabel || pairingRequest.accountId}
          </p>
        </div>
      </div>
      {error ? (
        <p role="alert" className="text-xs text-err">
          {error}
        </p>
      ) : null}
      <div className="flex justify-end gap-2">
        <Button
          variant="outline"
          size="sm"
          disabled={busy !== null}
          onClick={() => void decide("reject")}
          className="text-xs"
        >
          {busy === "reject" ? (
            <Loader2 size={14} className="animate-spin" />
          ) : null}
          Reject
        </Button>
        <Button
          size="sm"
          disabled={busy !== null}
          onClick={() => void decide("approve")}
          className="text-xs gap-1.5"
        >
          {busy === "approve" ? (
            <Loader2 size={14} className="animate-spin" />
          ) : (
            <ShieldCheck size={14} />
          )}
          Approve
        </Button>
      </div>
    </div>
  );
}

export function ChannelsStatusPanel() {
  // probe:true forces a live connectivity probe on the container rather than
  // returning the cached snapshot — the panel exists to show fresh health.
  const { data, error, isLoading, mutate } =
    useGatewayRpc<ChannelsStatusSnapshot>("channels.status", { probe: true });
  const pairing = useChannelPairingRequests();

  const channelOrder = data?.channelOrder ?? [];
  // The backend requests only Slack pairing records from the gateway.
  const pendingSlackRequests = pairing.data?.requests ?? [];

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

      {pendingSlackRequests.length > 0 ? (
        <section aria-labelledby="pending-slack-pairings" className="space-y-2">
          <div>
            <h3
              id="pending-slack-pairings"
              className="text-sm font-semibold text-[var(--midnight)]"
            >
              Pending Slack access
            </h3>
            <p className="text-xs text-[var(--midnight-muted)]">
              Approvals use Slack user and account identity. Pairing codes never
              appear here.
            </p>
          </div>
          {pendingSlackRequests.map((request) => (
            <PairingRequestCard
              key={request.requestId}
              pairingRequest={request}
              onResolved={() => pairing.refresh()}
            />
          ))}
        </section>
      ) : pairing.isLoading ? (
        <p className="flex items-center gap-1.5 text-xs text-[var(--midnight-muted)]">
          <Loader2 size={12} className="animate-spin" />
          Checking for pending Slack access requests…
        </p>
      ) : pairing.error ? (
        <div className="flex items-center gap-2">
          <p className="text-xs text-[var(--midnight-muted)]">
            Pending Slack access requests are temporarily unavailable.
          </p>
          <Button
            variant="outline"
            size="sm"
            onClick={() => pairing.refresh()}
            className="text-xs gap-1.5"
          >
            <RefreshCw size={12} />
            Try again
          </Button>
        </div>
      ) : null}

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
            No channels configured. Connect Slack from an agent&apos;s Channels
            tab, or add a Telegram/Discord bot there.
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
                  <p className="text-xs text-midnight-muted pl-1">
                    No accounts linked.
                  </p>
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
