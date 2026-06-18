"use client";

import { Button } from "@/components/ui/button";
import { useBilling } from "@/hooks/useBilling";
import { useConnections } from "@/hooks/useConnections";
import { ConnectButton } from "@/components/settings/ConnectButton";
import { INTEGRATION_RECEIPTS } from "@/components/settings/integration-receipts";

interface ConnectionsListProps {
  /** Org admins may connect/disconnect; non-admins see disabled actions. */
  canManage: boolean;
}

// The owner's inference path that unlocks `requires_bedrock` providers —
// matches billing_accounts.provider_choice (see backend connections.py).
const BEDROCK_PROVIDER_CHOICE = "bedrock_claude";

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
  // `requires_bedrock` providers are connectable only on Bedrock-managed
  // inference. One cached read (SWR-deduped) tells us the owner's path so we
  // can render those rows as locked-with-upsell instead of a dead Connect.
  //
  // Lock ONLY once billing has resolved. While it's still loading or errored,
  // `account` is undefined — locking then would briefly show the upsell to an
  // owner who is in fact on Bedrock. A resolved 404 (account === null, no
  // billing row) is a genuine "not on Bedrock" and does lock.
  const { account, isLoading: billingLoading, error: billingError } = useBilling();
  const offBedrock =
    !billingLoading &&
    !billingError &&
    account?.provider_choice !== BEDROCK_PROVIDER_CHOICE;

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
        // Lock only the connect path: a row already connected before a
        // provider switch keeps Disconnect (so `locked` excludes connected).
        // An owner who has configured their OWN OAuth app carries their own
        // data-use posture, so the Bedrock requirement is lifted server-side
        // (`_byo_active`) — excluding configured rows here keeps the UI in
        // lockstep, so a configured-but-not-connected owner falls through to
        // the normal Connect path instead of a dead-end "Switch to Bedrock".
        const locked =
          !!connection.requires_bedrock && !connected && offBedrock && !connection.byo_configured;
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
                {locked ? (
                  "Requires Bedrock credits"
                ) : (
                  <>
                    {connected ? "Connected" : "Not connected"}
                    {connection.acquisition === "direct_oauth" ? " · OAuth" : " · API key"}
                  </>
                )}
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
              lockedForBedrock={locked}
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
