"use client";

import { ConnectButton } from "@/components/settings/ConnectButton";
import {
  CompanyStripeSection,
  useCompanyStripeStatus,
} from "@/components/settings/CompanyStripeSection";
import { INTEGRATION_RECEIPTS } from "@/components/settings/integration-receipts";
import {
  ProvisionedResourcesFacet,
  accountLabel,
  useProvisioningRail,
} from "@/components/settings/ProvisionedResources";
import { reconnectReasonLine } from "@/components/settings/reconnectReason";
import { useConnections } from "@/hooks/useConnections";

/**
 * The consolidated Stripe section of the workspace Connections panel: ONE
 * brand, TWO separately-granted credentials, each with its own state and
 * action.
 *
 *   - "Company account" — the company's own Stripe account (Connect OAuth).
 *     Identity: revenue lands there and providers are provisioned on it.
 *     The credential is backend-only and never reaches a container.
 *   - "Agent tools"     — the agent-facing Stripe MCP integration. Its OAuth
 *     token lands in the container so agents get payment tools + triggers.
 *
 * The two credential planes stay fully separate: nothing here shares or
 * derives one from the other. The only cross-talk is COPY — when the
 * company account is connected, the agent grant hints to pick the same
 * Stripe account at sign-in.
 */
export function StripeSection({ canManage }: { canManage: boolean }) {
  const { data: companyStatus } = useCompanyStripeStatus();
  const companyBound = Boolean(companyStatus?.account_id);

  const { connections, mutate, connectPaste, startOAuth, disconnect } = useConnections();
  const rail = useProvisioningRail();
  const stripe = connections.find((c) => c.provider === "stripe");

  const authorized = stripe?.status === "connected";
  const connected = authorized && stripe?.runtime_supported !== false;
  const unavailable = stripe?.status === "unavailable";
  const receipt = stripe ? INTEGRATION_RECEIPTS[stripe.provider] : undefined;
  const stripeResources = rail.byProvider.get("stripe") ?? [];

  return (
    <section className="settings-card" data-testid="stripe-section">
      <div className="settings-card-header">
        <div>
          <h2 className="settings-card-title">Stripe</h2>
          <p className="settings-card-desc">
            Two separate grants for one Stripe: your company&apos;s own account, and the tools your
            agents use. Each connects on its own.
          </p>
        </div>
      </div>

      <div className="flex flex-col gap-3">
        <CompanyStripeSection canManage={canManage} />

        <div className="flex items-start gap-3 rounded-md border border-[var(--edge)] bg-cream p-4">
          <span
            className={`mt-1 h-2.5 w-2.5 shrink-0 rounded-full ${
              connected ? "bg-[var(--ok)]" : "bg-[var(--midnight-faint)]"
            }`}
            aria-hidden="true"
          />
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2">
              <h3 className="text-sm font-medium text-midnight">Agent tools</h3>
              {stripe?.actor === "mcp" && (
                <span className="shrink-0 rounded-sm border border-[var(--edge)] px-1.5 py-0.5 text-2xs font-medium text-[var(--midnight-muted)]">
                  MCP tools
                </span>
              )}
            </div>
            <p className="text-xs text-[var(--midnight-muted)]">
              {stripe ? (
                <>
                  {authorized && !connected
                    ? "Authorized · unavailable to agents"
                    : connected
                      ? "Connected"
                      : stripe.status === "reconnect_required"
                        ? "Needs reconnecting"
                        : unavailable
                        ? "Not available yet"
                        : "Not connected"}
                  {stripe.acquisition === "direct_oauth"
                    ? " · OAuth"
                    : stripe.acquisition === "aws_sts"
                      ? " · AWS role"
                      : " · API key"}
                </>
              ) : (
                "Not available yet"
              )}
            </p>
            {stripe && reconnectReasonLine(stripe) && (
              <p className="text-xs text-[var(--midnight-muted)] mt-0.5">{reconnectReasonLine(stripe)}</p>
            )}
            <p className="text-xs text-[var(--midnight-muted)] mt-0.5">
              What your agents can do in Stripe. A separate sign-in from the company account —
              agents only ever hold this grant, never the account credential.
            </p>
            {receipt && (
              <p className="text-xs text-[var(--midnight-muted)] mt-0.5">
                {connected
                  ? "Delivered: "
                  : authorized
                    ? "Agent access unavailable: "
                    : unavailable
                      ? "Would deliver: "
                      : "Will deliver: "}
                <span className={connected ? "text-[var(--ok)]" : undefined}>{receipt.delivers}</span>
              </p>
            )}
            {stripe?.can_trigger && stripe.can_trigger.length > 0 && (
              <p className="text-xs text-[var(--midnight-muted)] mt-0.5">
                Can trigger: {stripe.can_trigger.slice(0, 3).join(", ")}
                {stripe.can_trigger.length > 3 && ` and ${stripe.can_trigger.length - 3} more`}
              </p>
            )}
            {/* Spec §3.2 (same as the list rows): Projects hands over the
                provider's WRITE token, so resources can exist while the
                read-side OAuth is still disconnected. */}
            {stripe?.status === "disconnected" && stripeResources.length > 0 && (
              <p className="text-xs text-[var(--midnight-muted)] mt-0.5">
                Also connect this grant for reads — your agents can create things here, but
                can&apos;t read your data back until you connect it.
                {accountLabel(rail.accountByProvider.get("stripe")) &&
                  ` The account we set up is ${accountLabel(rail.accountByProvider.get("stripe"))}.`}
              </p>
            )}
            {/* Copy-only hint — no credential crosses between the grants. */}
            {companyBound && stripe?.status === "disconnected" && !stripe.requestable && (
              <p className="text-xs text-[var(--midnight-muted)] mt-0.5">
                When Stripe asks you to sign in, pick the same account you connected above — your
                agents then work with the same customers and payments.
              </p>
            )}
            <ProvisionedResourcesFacet
              provider="stripe"
              resources={stripeResources}
              workflows={rail.workflowsByProvider.get("stripe") ?? []}
              canManage={canManage}
              onChanged={rail.refresh}
            />
          </div>
          {stripe && (
            <ConnectButton
              connection={stripe}
              canManage={canManage}
              connectPaste={connectPaste}
              startOAuth={startOAuth}
              disconnect={disconnect}
              refreshConnections={mutate}
            />
          )}
        </div>
      </div>
    </section>
  );
}
