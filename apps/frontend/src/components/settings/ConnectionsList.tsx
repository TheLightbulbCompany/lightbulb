"use client";

import { Button } from "@/components/ui/button";
import { useConnections } from "@/hooks/useConnections";
import { ConnectButton } from "@/components/settings/ConnectButton";
import { INTEGRATION_RECEIPTS } from "@/components/settings/integration-receipts";

interface ConnectionsListProps {
  /** Org admins may connect/disconnect; non-admins see disabled actions. */
  canManage: boolean;
}

/**
 * Renders one row per provider returned by GET /connections: a status dot
 * (connected = green / disconnected = muted), the provider's display name,
 * and the right action via ConnectButton. The provider list is fully
 * server-driven — nothing here is hardcoded.
 *
 * Holds no mutation state of its own: ConnectButton owns per-row busy /
 * error state, and useConnections owns the SWR cache + mutate. The
 * /settings/connections page re-mutates this same SWR key after an OAuth
 * return so a freshly-connected provider flips to green without a reload.
 */
export function ConnectionsList({ canManage }: ConnectionsListProps) {
  const { connections, isLoading, error, mutate, connectPaste, startOAuth, disconnect } =
    useConnections();

  if (error) {
    return (
      <div className="space-y-2">
        <p className="text-sm text-[#b63025]">Failed to load connections.</p>
        <Button variant="outline" size="sm" onClick={() => mutate()}>
          Retry
        </Button>
      </div>
    );
  }

  if (isLoading) {
    return <div className="text-sm text-[#8a8578]">Loading connections…</div>;
  }

  if (connections.length === 0) {
    return (
      <div className="rounded-md border border-[#e0dbd0] p-4 text-sm text-[#8a8578]">
        No integrations are available yet.
      </div>
    );
  }

  return (
    <div className="space-y-2">
      {connections.map((connection) => {
        const connected = connection.status === "connected";
        const receipt = INTEGRATION_RECEIPTS[connection.provider];
        return (
          <div
            key={connection.provider}
            className="flex items-center gap-3 rounded-md border border-[#e0dbd0] bg-white p-4"
          >
            <span
              className={`h-2.5 w-2.5 flex-shrink-0 rounded-full ${
                connected ? "bg-[#2d8a4e]" : "bg-[#c5bfb6]"
              }`}
              aria-hidden="true"
            />
            <div className="flex-1 min-w-0">
              <p className="text-sm font-medium text-[#1c1917]">{connection.display_name}</p>
              <p className="text-xs text-[#8a8578]">
                {connected ? "Connected" : "Not connected"}
                {connection.acquisition === "direct_oauth"
                  ? " · OAuth"
                  : connection.acquisition === "aws_sts"
                    ? " · AWS role"
                    : " · API key"}
              </p>
              {receipt && (
                <p className="text-xs text-[#8a8578] mt-0.5">
                  {connected ? "Delivered: " : "Will deliver: "}
                  <span className={connected ? "text-[#2d8a4e]" : undefined}>
                    {receipt.delivers}
                  </span>
                </p>
              )}
            </div>
            <ConnectButton
              connection={connection}
              canManage={canManage}
              connectPaste={connectPaste}
              startOAuth={startOAuth}
              disconnect={disconnect}
              refreshConnections={mutate}
            />
          </div>
        );
      })}
    </div>
  );
}
