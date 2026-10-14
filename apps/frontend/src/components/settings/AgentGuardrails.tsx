"use client";

// What agents may do, and the kill switch. This is what remains of the retired
// Accounts tab: its other two sections (where money lands, what agents can use)
// were already on Connections, so this sits at the foot of Settings →
// Connections, under the one list of what the team is connected to.
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
import { ShieldAlert } from "lucide-react";

import { PanelSection, EditorialDialog, EditorialButton, EditorialInput } from "@/components/teams/shell";
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

export function AgentGuardrails() {
  const api = useApi();
  const { mutate: globalMutate } = useSWRConfig();
  // UX gate only — every write below is re-enforced server-side (403 for a
  // non-admin org member, `company_policy._owner`).
  const canManage = useIsWorkspaceAdmin();

  const [open, setOpen] = useState(true);
  const [revokeOpen, setRevokeOpen] = useState(false);
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);

  const policy = useSWR<CapabilityPolicy>(
    POLICY_PATH,
    (p: string) => api.get(p) as Promise<CapabilityPolicy>,
  );

  // Hoisted so the JSX below narrows — a property read off the SWR result does not.
  const policyMap = policy.data;

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
    <div className="flex flex-col gap-4">
      <PanelSection
        label="Guardrails"
        open={open}
        onOpenChange={setOpen}
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
