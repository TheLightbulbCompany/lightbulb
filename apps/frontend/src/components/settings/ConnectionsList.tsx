"use client";

import { Button } from "@/components/ui/button";
import { useConnections, type Connection } from "@/hooks/useConnections";
import { ConnectButton } from "@/components/settings/ConnectButton";
import { INTEGRATION_RECEIPTS } from "@/components/settings/integration-receipts";
import {
  ProvisionedResourcesFacet,
  accountLabel,
  useProvisioningRail,
} from "@/components/settings/ProvisionedResources";

interface ConnectionsListProps {
  /** Org admins may connect/disconnect; non-admins see disabled actions. */
  canManage: boolean;
  /**
   * Providers rendered elsewhere on the same surface (e.g. Stripe's
   * consolidated section in the Connections panel) — dropped from this list so
   * one brand never shows up twice. Omitted everywhere else.
   */
  exclude?: string[];
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
 * The two "dashboard access" paste rows: a second Attio/Apollo key the
 * workspace would hold for dashboard reads nothing makes. The registry keeps
 * them (its tests pin their wiring); no list offers them.
 */
const HIDDEN_PROVIDERS: ReadonlySet<string> = new Set(["attio_api", "apollo_api"]);

/**
 * Renders one row per provider returned by GET /connections: a status dot
 * (connected = green / disconnected = muted), the provider's display name,
 * and the right action via ConnectButton. The provider list is fully
 * server-driven — nothing here is hardcoded.
 *
 * Holds no mutation state of its own: ConnectButton owns per-row busy /
 * error state, and useConnections owns the SWR cache + mutate. The
 * Connections panel re-mutates this same SWR key after an OAuth return so a
 * freshly-connected provider flips to green without a reload.
 */
export function ConnectionsList({ canManage, exclude }: ConnectionsListProps) {
  const { connections, isLoading, error, mutate, connectPaste, startOAuth, disconnect } =
    useConnections();
  // The second facet. Its providers are NOT the OAuth registry's: this rail
  // provisions neon/supabase/resend, which have no Connections row at all, so
  // they get a card of their own rather than disappearing.
  const rail = useProvisioningRail();
  const railOnlyProviders = rail.providers.filter(
    (p) => !connections.some((c) => c.provider === p),
  );
  const rows: { connection: Connection; railOnly: boolean }[] = [
    ...connections.map((connection) => ({ connection, railOnly: false })),
    ...railOnlyProviders.map((provider) => ({
      connection: {
        provider,
        display_name: provider,
        acquisition: "paste_token",
        status: "disconnected",
      } as Connection,
      railOnly: true,
    })),
  ].filter((r) => !exclude?.includes(r.connection.provider) && !HIDDEN_PROVIDERS.has(r.connection.provider));

  if (error) {
    return (
      <div className="space-y-2">
        <p className="text-sm text-[var(--err)]">Failed to load connections.</p>
        <Button variant="outline" size="sm" onClick={() => mutate()}>
          Retry
        </Button>
      </div>
    );
  }

  // The rail's own read can fail while the OAuth catalog loads fine. Both
  // branches below show it, because an empty rail and a failed one look
  // identical otherwise.
  const railError = rail.error ? (
    <p className="text-sm text-[var(--err)]">Couldn&apos;t load your resources. Refresh to try again.</p>
  ) : null;

  if (isLoading) {
    return <div className="text-sm text-[var(--midnight-muted)]">Loading connections…</div>;
  }

  if (rows.length === 0) {
    return (
      <div className="space-y-2">
        {railError}
        <div className="rounded-md border border-[var(--edge)] p-4 text-sm text-[var(--midnight-muted)]">
          No integrations are available yet.
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-2">
      {railError}
      {rows.map(({ connection, railOnly }) => {
        const authorized = connection.status === "connected";
        const connected = authorized && connection.runtime_supported !== false;
        // Gated off this owner. Distinct from "not connected": there is nothing
        // they can click to change it, so the row must not read as one step away
        // — and must not promise capabilities we cannot deliver to them yet.
        const unavailable = connection.status === "unavailable";
        // `container_login` (Shopify) ran the CLI's OAuth grant inside the
        // OpenClaw container and finished it in the Browser panel — neither
        // exists any more. A row connected before the runtime move keeps its
        // Disconnect action (`ConnectButton` checks `status === "connected"`
        // before it ever looks at `acquisition`); only a NEW connection attempt
        // is unavailable.
        const unavailableOnRuntime =
          connection.runtime_supported === false ||
          (connection.acquisition === "container_login" && !authorized);
        const receipt = INTEGRATION_RECEIPTS[connection.provider];
        return (
          <div
            key={connection.provider}
            className="flex items-center gap-3 rounded-md border border-[var(--edge)] bg-cream p-4"
          >
            <span
              className={`h-2.5 w-2.5 shrink-0 rounded-full ${
                connected ? "bg-[var(--ok)]" : "bg-[var(--midnight-faint)]"
              }`}
              aria-hidden="true"
            />
            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-2">
                <p className="text-sm font-medium text-midnight">
                  {connection.display_name}
                </p>
                {connection.actor && KIND_LABELS[connection.actor] && (
                  <span className="shrink-0 rounded-sm border border-[var(--edge)] px-1.5 py-0.5 text-2xs font-medium text-[var(--midnight-muted)]">
                    {KIND_LABELS[connection.actor]}
                  </span>
                )}
              </div>
              <p className="text-xs text-[var(--midnight-muted)]">
                {railOnly ? (
                  "Set up by your agents on your own provider account"
                ) : authorized && unavailableOnRuntime ? (
                  "Authorized · unavailable to agents"
                ) : unavailableOnRuntime ? (
                  "Not available on this runtime"
                ) : (
                  <>
                    {connected ? "Connected" : unavailable ? "Not available yet" : "Not connected"}
                    {connection.acquisition === "direct_oauth"
                      ? " · OAuth"
                      : connection.acquisition === "aws_sts"
                        ? " · AWS role"
                        : " · API key"}
                  </>
                )}
              </p>
              {receipt && (
                <p className="text-xs text-[var(--midnight-muted)] mt-0.5">
                  {connected
                    ? "Delivered: "
                    : authorized && unavailableOnRuntime
                      ? "Agent access unavailable: "
                      : unavailable
                        ? "Would deliver: "
                        : "Will deliver: "}
                  <span className={connected ? "text-[var(--ok)]" : undefined}>
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
              {/* Spec §3.2: Projects hands over the provider's WRITE token, so a
                  provider can hold resources while its read-side OAuth is still
                  disconnected. Say so rather than leaving the dot muted. */}
              {!railOnly &&
                connection.status === "disconnected" &&
                (rail.byProvider.get(connection.provider)?.length ?? 0) > 0 && (
                  <p className="text-xs text-[var(--midnight-muted)] mt-0.5">
                    Also connect this account for reads — your agents can create things here, but
                    can&apos;t read your data back until you connect it.
                    {/* WHICH account to sign in as. Without it the owner is told
                        to connect "posthog" and has to guess which of their
                        logins this rail actually used. */}
                    {accountLabel(rail.accountByProvider.get(connection.provider)) &&
                      ` The account we set up is ${accountLabel(rail.accountByProvider.get(connection.provider))}.`}
                  </p>
                )}
              <ProvisionedResourcesFacet
                provider={connection.provider}
                resources={rail.byProvider.get(connection.provider) ?? []}
                workflows={rail.workflowsByProvider.get(connection.provider) ?? []}
                canManage={canManage}
                onChanged={rail.refresh}
              />
            </div>
            {!railOnly &&
              (unavailableOnRuntime && !authorized ? null : (
                <ConnectButton
                  connection={connection}
                  canManage={canManage}
                  connectPaste={connectPaste}
                  startOAuth={startOAuth}
                  disconnect={disconnect}
                  refreshConnections={mutate}
                />
              ))}
          </div>
        );
      })}
    </div>
  );
}
