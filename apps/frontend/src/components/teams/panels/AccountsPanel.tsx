"use client";

// apps/frontend/src/components/teams/panels/AccountsPanel.tsx
//
// The owner's answer to: "what does my company have accounts with, which of
// those did an agent set up on my behalf, and how do I stop it." Composition
// only — every read here is already owned by a Settings surface, and SWR
// dedupes the shared keys, so mounting this panel costs no extra requests
// beyond the policy row.
//
// Structural pattern: ArtifactsPanel.tsx (editorial-page wrapper + PanelMasthead
// + one section per concern).
//
// ⚠️ HONESTY NOTE — the Guardrails switches are RECORDED, NOT ENFORCED.
// `routers/company_policy.py` lines 3-6 state it outright: the map is stored on
// the billing row and no rail reads it yet; enforcement is P1. The section says
// so IN THE UI (not just in this comment), because an owner who believes a
// switch is a fence is worse off than one who sees no switch at all. The
// control that actually bites today is "Revoke everything", which composes live
// primitives: cancel open provisioning workflows, delete our copies of the
// credentials, sever the Stripe Connect link.

import { useState } from "react";
import useSWR, { useSWRConfig } from "swr";
import { Plug, ShieldAlert } from "lucide-react";

import { PanelMasthead, PanelSection, PanelEmptyState, EditorialDialog, EditorialButton, EditorialInput } from "@/components/teams/shell";
import { CompanyStripeSection } from "@/components/settings/CompanyStripeSection";
import { ConnectionsList } from "@/components/settings/ConnectionsList";
import { useProvisioningRail } from "@/components/settings/ProvisionedResources";
import { useConnections } from "@/hooks/useConnections";
import { useIsWorkspaceAdmin } from "@/hooks/useIsWorkspaceAdmin";
import { useApi } from "@/lib/api";
import { notifyApiError, notifyError, notifySuccess } from "@/lib/notify";
import { cn } from "@/lib/utils";

const POLICY_PATH = "/company/policy";

/** The exact string `POST /company/revoke-all` compares against — anything else is a 422. */
const REVOKE_CONFIRM = "revoke";

type CapabilityPolicy = {
  money_moves: boolean;
  outreach: boolean;
  publishing: boolean;
  provisioning: boolean;
};

// Keys mirror billing_repo.CAPABILITY_POLICY_KEYS; `true` = "ask me first"
// (the backend fills an absent key as true, fail-closed).
const POLICY_ROWS: { key: keyof CapabilityPolicy; label: string; detail: string }[] = [
  { key: "money_moves", label: "Moving money", detail: "Paying for a service, issuing a refund, changing a price." },
  { key: "outreach", label: "Contacting people", detail: "Emailing or messaging anyone outside the company." },
  { key: "publishing", label: "Publishing", detail: "Posting anything the public can see." },
  { key: "provisioning", label: "Signing up for services", detail: "Creating an account or resource on a provider." },
];

function PolicySwitch({
  checked,
  disabled,
  label,
  onToggle,
}: {
  checked: boolean;
  disabled?: boolean;
  label: string;
  onToggle: (next: boolean) => void;
}) {
  // No shared Switch primitive exists in ui/ — same styled role="switch"
  // button RoutinesPanel uses for its enable toggle.
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={`${checked ? "Stop asking" : "Ask me"} before ${label.toLowerCase()}`}
      disabled={disabled}
      onClick={() => onToggle(!checked)}
      className={cn(
        "relative inline-flex h-5 w-9 shrink-0 items-center rounded-full border transition-colors",
        "disabled:cursor-not-allowed disabled:opacity-50",
      )}
      style={{
        backgroundColor: checked ? "var(--midnight)" : "var(--cream-deep)",
        borderColor: checked ? "var(--midnight)" : "var(--edge)",
      }}
    >
      <span
        aria-hidden="true"
        className={cn(
          "inline-block h-3.5 w-3.5 transform rounded-full bg-cream shadow-sm transition-transform",
          checked ? "translate-x-[18px]" : "translate-x-0.5",
        )}
      />
    </button>
  );
}

export function AccountsPanel() {
  const api = useApi();
  const { mutate: globalMutate } = useSWRConfig();
  // UX gate only — every write below is re-enforced server-side (403 for a
  // non-admin org member, `company_policy._owner`).
  const canManage = useIsWorkspaceAdmin();

  const [open, setOpen] = useState({ account: true, connected: true, guardrails: true });
  const [revokeOpen, setRevokeOpen] = useState(false);
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);

  const { connections } = useConnections();
  // Same SWR keys ConnectionsList reads — deduped, not a second round trip.
  const rail = useProvisioningRail();
  const policy = useSWR<CapabilityPolicy>(
    POLICY_PATH,
    (p: string) => api.get(p) as Promise<CapabilityPolicy>,
  );

  // Hoisted so the JSX below narrows — a property read off the SWR result does not.
  const policyMap = policy.data;

  // What the masthead counts: providers with something actually wired — a
  // connected OAuth row, or a resource this rail created on the owner's own
  // account. The company Stripe account is its own section right below, so it
  // is deliberately not folded into this number.
  const wired = new Set([
    ...connections.filter((c) => c.status === "connected" && c.runtime_supported !== false).map((c) => c.provider),
    ...rail.providers,
  ]);

  async function togglePolicy(key: keyof CapabilityPolicy, next: boolean) {
    const current = policy.data;
    if (!current) return;
    setBusy(true);
    try {
      // Optimistic + rollbackOnError: a 422/404 puts the switch back where it
      // was without any local mirror of the map.
      await policy.mutate(async () => (await api.patch(POLICY_PATH, { [key]: next })) as CapabilityPolicy, {
        optimisticData: { ...current, [key]: next },
        rollbackOnError: true,
        revalidate: false,
      });
    } catch (e) {
      notifyApiError(e, "Couldn't save that preference.");
    } finally {
      setBusy(false);
    }
  }

  async function revokeAll() {
    setBusy(true);
    try {
      const res = (await api.post("/company/revoke-all", { confirm: REVOKE_CONFIRM })) as {
        revoked?: string[];
        failed?: string[];
      };
      setRevokeOpen(false);
      setTyped("");
      notifySuccess(`Revoked ${res.revoked?.length ?? 0} item(s).`);
      if (res.failed?.length) {
        notifyError(`Still connected: ${res.failed.join(", ")}. Try again, or disconnect them below.`);
      }
      // Everything on this page just changed shape — revalidate the lot rather
      // than naming four SWR keys that live in three other modules.
      void globalMutate(() => true);
    } catch (e) {
      notifyApiError(e, "Couldn't revoke access. Try again.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="editorial-page p-4 sm:p-5 lg:p-6 flex flex-col gap-4 max-w-4xl mx-auto w-full">
      <PanelMasthead
        overline="Company"
        title="Accounts"
        meta={`${wired.size} connected`}
      />

      {/* Two rails, one brand. The company account below is the owner's own
          Stripe Connect merchant account — backend-only credential, revenue
          lands there. The "Stripe" row in the next section is a direct_oauth
          MCP grant whose token lives in the container. Framed as "Company
          account" over "Connected" they read as one thing in two states, and
          an owner duly concluded their Stripe integration was connected
          because this page said so. Both sections are therefore labelled by
          what they DO, and each says in the owner's own language that it is
          not the other. Same vocabulary as StripeSection on
          /settings/connections — that component can't be reused here, its
          `.settings-card` classes live in the route-scoped settings.css and
          are never loaded on /chat (see TeamBillingCard's styling note).

          THE RULE TO KEEP: while these two share a page they must not share a
          vocabulary. The bare word "Stripe" may never be the label a
          connection status hangs off, or "Not connected" reads as a verdict on
          the owner's money — see the ConnectionsList `labels` call below. */}
      <PanelSection
        label="Where money lands"
        open={open.account}
        onOpenChange={(v) => setOpen((s) => ({ ...s, account: v }))}
      >
        <div className="flex flex-col gap-3 py-3">
          <p className="text-caption" style={{ color: "var(--midnight-muted)" }}>
            Your own Stripe account. Revenue lands here, and anything we set up for you is billed
            to it. This is not an integration — your agents never hold its credentials.
          </p>
          <CompanyStripeSection canManage={canManage} />
        </div>
      </PanelSection>

      {/* No `count` here on purpose. Under "Connected" the bare number read as
          "N connected"; under a what-it-does label it would be a number
          against no denominator, next to a list where most rows are not
          connected. The masthead already carries "N connected". */}
      <PanelSection
        label="What your agents can use"
        open={open.connected}
        onOpenChange={(v) => setOpen((s) => ({ ...s, connected: v }))}
      >
        <div className="flex flex-col gap-3 py-3">
          <p className="text-caption" style={{ color: "var(--midnight-muted)" }}>
            Separate sign-ins your agents use to do work. Each is its own connection — none of
            them is your company account above, and connecting one never changes where your
            revenue lands.
          </p>
          {/* The one thing this page says with no data: your agents have not
              set anything up on your behalf. Reassurance, not an error — so it
              sits above the catalog rather than replacing it. */}
          {rail.providers.length === 0 && !rail.error && (
            <PanelEmptyState
              icon={Plug}
              headline="Nothing provisioned yet."
              supporting="When Friday sets up a service for you, it lands here with a link to that provider's own dashboard."
              className="py-8"
            />
          )}
          {/* The row must survive being read on its own, because the status
              sits right next to it: "Stripe · Not connected" one section under
              a company Stripe account is how an owner concludes the upgrade
              they just paid for failed, and then "fixes" it by connecting the
              MCP grant — which leaves them OAuth-connected with still no
              company account. The brand name alone never carries a status
              here. */}
          <ConnectionsList canManage={canManage} labels={{ stripe: "Stripe for your agents" }} />
        </div>
      </PanelSection>

      <PanelSection
        label="Guardrails"
        open={open.guardrails}
        onOpenChange={(v) => setOpen((s) => ({ ...s, guardrails: v }))}
      >
        <div className="flex flex-col gap-3 py-3">
          {/* Not a footnote. The switches below are a recorded preference, and
              the owner has to know that before they read one as a fence. */}
          <p
            role="note"
            className="rounded-md border p-3 text-ui"
            style={{
              borderColor: "var(--edge)",
              background: "var(--cream-deep)",
              color: "var(--midnight-muted)",
            }}
          >
            <span style={{ color: "var(--midnight)" }}>Recorded, not yet enforced.</span> We store
            these preferences on your account and your agents will follow them once approval
            enforcement ships. Today they change nothing on their own — to actually cut off access,
            use Revoke everything below.
          </p>

          {policy.error && !policy.data && (
            <p className="text-ui" style={{ color: "var(--err)" }}>
              Couldn&apos;t load your guardrails. Refresh to try again.
            </p>
          )}

          {policyMap &&
            POLICY_ROWS.map((row) => (
              <div key={row.key} className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <p className="text-ui" style={{ color: "var(--midnight)" }}>
                    Ask me first: {row.label}
                  </p>
                  <p className="text-caption" style={{ color: "var(--midnight-muted)" }}>
                    {row.detail}
                  </p>
                </div>
                {canManage ? (
                  <PolicySwitch
                    checked={policyMap[row.key]}
                    disabled={busy}
                    label={row.label}
                    onToggle={(next) => void togglePolicy(row.key, next)}
                  />
                ) : (
                  <span className="text-caption" style={{ color: "var(--midnight-muted)" }}>
                    {policyMap[row.key] ? "Ask first" : "Don’t ask"}
                  </span>
                )}
              </div>
            ))}

          {!canManage && (
            <p className="text-caption" style={{ color: "var(--midnight-muted)" }}>
              A workspace admin changes these.
            </p>
          )}

          {canManage && (
            <div className="mt-1 flex flex-col gap-2 border-t pt-3" style={{ borderColor: "var(--edge)" }}>
              <p className="text-ui" style={{ color: "var(--midnight)" }}>
                Revoke everything
              </p>
              <p className="text-caption" style={{ color: "var(--midnight-muted)" }}>
                Cancels open requests, deletes our copies of your credentials, and disconnects
                your connected apps and Stripe. It does not cancel or delete anything you already own at those providers.
              </p>
              <div>
                <EditorialButton
                  variant="destructive"
                  size="sm"
                  disabled={busy}
                  onClick={() => setRevokeOpen(true)}
                >
                  Revoke everything
                </EditorialButton>
              </div>
            </div>
          )}
        </div>
      </PanelSection>

      <EditorialDialog
        open={revokeOpen}
        onOpenChange={(next) => {
          if (!next && !busy) {
            setRevokeOpen(false);
            setTyped("");
          }
        }}
        title="Revoke everything?"
        description="This cancels open requests, deletes our copies of your credentials, and disconnects your connected apps and Stripe. It does not cancel or delete anything you already own at those providers."
        footer={
          <>
            <EditorialButton
              variant="secondary"
              size="sm"
              disabled={busy}
              onClick={() => {
                setRevokeOpen(false);
                setTyped("");
              }}
            >
              Cancel
            </EditorialButton>
            <EditorialButton
              variant="destructive"
              size="sm"
              // Typed confirmation, not a bare button: the backend demands the
              // same exact string (422 otherwise), so the gate is real on both
              // sides rather than decorative here.
              disabled={busy || typed.trim() !== REVOKE_CONFIRM}
              onClick={() => void revokeAll()}
            >
              {busy ? "Revoking…" : "Revoke everything"}
            </EditorialButton>
          </>
        }
      >
        <div className="flex items-start gap-2">
          <ShieldAlert size={16} aria-hidden="true" style={{ color: "var(--err)" }} className="mt-0.5 shrink-0" />
          <p className="text-caption" style={{ color: "var(--midnight-muted)" }}>
            Your agents lose every credential we hold for them until you connect things again.
          </p>
        </div>
        <EditorialInput
          label={`Type ${REVOKE_CONFIRM} to confirm`}
          autoComplete="off"
          spellCheck={false}
          value={typed}
          onChange={(e) => setTyped(e.target.value)}
        />
      </EditorialDialog>
    </div>
  );
}
