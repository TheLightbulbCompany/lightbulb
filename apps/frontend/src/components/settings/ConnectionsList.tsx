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
 * Human label for the integration's rail (`actor` from the registry). The
 * badge answers "where will this show up once connected" — MCP integrations
 * appear as tools in the agent's tool list, CLI ones (gh/aws/gog) are used
 * from the agent's shell and never appear as tools, skills read the
 * credential from a bundled skill. Unknown/absent actor renders no badge.
 */
const KIND_LABELS: Record<string, string> = {
  mcp: "MCP tools",
  cli: "CLI",
  skill: "Skill",
};

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
    return <div className="text-sm text-[var(--midnight-muted)]">Loading connections…</div>;
  }

  if (connections.length === 0) {
    return (
      <div className="rounded-md border border-[var(--edge)] p-4 text-sm text-[var(--midnight-muted)]">
        No integrations are available yet.
      </div>
    );
  }

  return (
    <div className="space-y-2">
      {connections.map((connection) => {
        const connected = connection.status === "connected";
        // Gated off this owner. Distinct from "not connected": there is nothing
        // they can click to change it, so the row must not read as one step away
        // — and must not promise capabilities we cannot deliver to them yet.
        const unavailable = connection.status === "unavailable";
        const receipt = INTEGRATION_RECEIPTS[connection.provider];
        return (
          <div
            key={connection.provider}
            className="flex items-center gap-3 rounded-md border border-[var(--edge)] bg-white p-4"
          >
            <span
              className={`h-2.5 w-2.5 flex-shrink-0 rounded-full ${
                connected ? "bg-[#2d8a4e]" : "bg-[#c5bfb6]"
              }`}
              aria-hidden="true"
            />
            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-2">
                <p className="text-sm font-medium text-[#1c1917]">{connection.display_name}</p>
                {connection.actor && KIND_LABELS[connection.actor] && (
                  <span className="flex-shrink-0 rounded border border-[var(--edge)] px-1.5 py-0.5 text-[10px] font-medium text-[var(--midnight-muted)]">
                    {KIND_LABELS[connection.actor]}
                  </span>
                )}
              </div>
              <p className="text-xs text-[var(--midnight-muted)]">
                {connected ? "Connected" : unavailable ? "Not available yet" : "Not connected"}
                {connection.acquisition === "direct_oauth"
                  ? " · OAuth"
                  : connection.acquisition === "aws_sts"
                    ? " · AWS role"
                    : " · API key"}
              </p>
              {receipt && (
                <p className="text-xs text-[var(--midnight-muted)] mt-0.5">
                  {connected ? "Delivered: " : unavailable ? "Would deliver: " : "Will deliver: "}
                  <span className={connected ? "text-[#2d8a4e]" : undefined}>
                    {receipt.delivers}
                  </span>
                </p>
              )}
              {connection.can_trigger && connection.can_trigger.length > 0 && (
                <p className="text-xs text-[var(--midnight-muted)] mt-0.5">
                  {/* Providers carry full event catalogs now (Stripe ~265) —
                      show the curated leaders (the registry orders those
                      first) and count the rest. */}
                  Can trigger: {connection.can_trigger.slice(0, 3).join(", ")}
                  {connection.can_trigger.length > 3 &&
                    ` and ${connection.can_trigger.length - 3} more`}
                </p>
              )}
              {connection.actor === "cli" && (
                <p className="text-xs text-[var(--midnight-muted)] mt-0.5">
                  Works through the agent&apos;s command line — it won&apos;t appear in the
                  agent&apos;s tool list.
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
