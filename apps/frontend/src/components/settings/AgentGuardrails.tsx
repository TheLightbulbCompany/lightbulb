"use client";

// What agents may do, and the kill switch. This is what remains of the retired
// Accounts tab: its other two sections (where money lands, what agents can use)
// were already on Connections, so this sits at the foot of Settings →
// Connections, under the one list of what the team is connected to.
//
// The approval mode (founder ruling 6 Nov 2026) replaced the four "Ask me
// first" switches: one choice per workspace, read on the server by the effects
// route (`runtime_internal.effect_check`), so an agent can never change it.
// The old keys stay stored: `outreach`/`money_moves` still pick who answers a
// card. Never ask is admin-only behind a typed word, checked on both sides.

import { useState } from "react";
import useSWR, { useSWRConfig } from "swr";
import { ShieldAlert } from "lucide-react";

import { PanelSection, EditorialDialog, EditorialButton, EditorialInput } from "@/components/teams/shell";
import { useIsWorkspaceAdmin } from "@/hooks/useIsWorkspaceAdmin";
import { useApi } from "@/lib/api";
import { notifyApiError, notifyError, notifySuccess } from "@/lib/notify";

const POLICY_PATH = "/company/policy";

/** The exact string `POST /company/revoke-all` compares against — anything else is a 422. */
const REVOKE_CONFIRM = "revoke";

/** The exact word `PATCH /company/policy` wants before it stores Never ask (422 otherwise). */
const NEVER_ASK_CONFIRM = "never ask";

type ApprovalMode = "ask" | "auto" | "never_ask";
type CapabilityPolicy = { approval_mode: ApprovalMode };

const MODES: { value: ApprovalMode; label: string; detail: string }[] = [
  { value: "ask", label: "Ask", detail: "Agents ask before every outside action: sending, spending, publishing, deleting." },
  {
    value: "auto",
    label: "Auto",
    detail: "Agents ask only before mailing or texting people outside the workspace, spending over your purchase limit, or deleting outside data.",
  },
  { value: "never_ask", label: "Never ask", detail: "Agents never ask. Every action is still logged, and spending limits still apply." },
];

export function AgentGuardrails() {
  const api = useApi();
  const { mutate: globalMutate } = useSWRConfig();
  // UX gate only — every write below is re-enforced server-side (403 for a
  // non-admin org member, `company_policy._owner`).
  const canManage = useIsWorkspaceAdmin();

  const [open, setOpen] = useState(true);
  const [revokeOpen, setRevokeOpen] = useState(false);
  const [typed, setTyped] = useState("");
  const [neverOpen, setNeverOpen] = useState(false);
  const [neverTyped, setNeverTyped] = useState("");
  const [busy, setBusy] = useState(false);

  const policy = useSWR<CapabilityPolicy>(
    POLICY_PATH,
    (p: string) => api.get(p) as Promise<CapabilityPolicy>,
  );

  // Hoisted so the JSX below narrows — a property read off the SWR result does not.
  const policyMap = policy.data;

  async function setMode(mode: ApprovalMode) {
    const current = policy.data;
    if (!current || current.approval_mode === mode) return;
    if (mode === "never_ask" && !neverOpen) {
      setNeverOpen(true);
      return;
    }
    setBusy(true);
    try {
      const body = mode === "never_ask" ? { approval_mode: mode, confirm: NEVER_ASK_CONFIRM } : { approval_mode: mode };
      // Optimistic + rollbackOnError: a 422/403 puts the choice back.
      await policy.mutate(async () => (await api.patch(POLICY_PATH, body)) as CapabilityPolicy, {
        optimisticData: { ...current, approval_mode: mode },
        rollbackOnError: true,
        revalidate: false,
      });
      setNeverOpen(false);
      setNeverTyped("");
    } catch (e) {
      notifyApiError(e, "Couldn't change how agents ask.");
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
          {policy.error && !policy.data && (
            <p className="text-ui" style={{ color: "var(--err)" }}>
              Couldn&apos;t load your guardrails. Refresh to try again.
            </p>
          )}

          {policyMap && (
            <fieldset className="flex flex-col gap-2" disabled={!canManage || busy}>
              <legend className="mb-1 text-ui" style={{ color: "var(--midnight)" }}>
                When agents ask first
              </legend>
              {MODES.map((m) => (
                <label key={m.value} className="flex cursor-pointer items-start gap-3">
                  <input
                    type="radio"
                    name="approval-mode"
                    value={m.value}
                    checked={policyMap.approval_mode === m.value}
                    onChange={() => void setMode(m.value)}
                    className="mt-1"
                  />
                  <span className="min-w-0">
                    <span className="block text-ui" style={{ color: "var(--midnight)" }}>
                      {m.label}
                      {m.value === "auto" && <span style={{ color: "var(--midnight-muted)" }}> (default)</span>}
                    </span>
                    <span className="block text-caption" style={{ color: "var(--midnight-muted)" }}>
                      {m.detail}
                    </span>
                  </span>
                </label>
              ))}
              <p className="text-caption" style={{ color: "var(--midnight-muted)" }}>
                Anyone who answers a request can choose Always allow for that recipient, app or repo.
              </p>
            </fieldset>
          )}

          {!canManage && (
            <p className="text-caption" style={{ color: "var(--midnight-muted)" }}>
              A workspace admin changes this.
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

      <EditorialDialog
        open={neverOpen}
        onOpenChange={(next) => {
          if (!next && !busy) {
            setNeverOpen(false);
            setNeverTyped("");
          }
        }}
        title="Turn on Never ask?"
        description="Agents will mail people outside the workspace, spend within your limits and delete outside data without asking anyone. Every action is still logged."
        footer={
          <>
            <EditorialButton
              variant="secondary"
              size="sm"
              disabled={busy}
              onClick={() => {
                setNeverOpen(false);
                setNeverTyped("");
              }}
            >
              Cancel
            </EditorialButton>
            <EditorialButton
              variant="destructive"
              size="sm"
              disabled={busy || neverTyped.trim() !== NEVER_ASK_CONFIRM}
              onClick={() => void setMode("never_ask")}
            >
              {busy ? "Saving…" : "Turn on Never ask"}
            </EditorialButton>
          </>
        }
      >
        <EditorialInput
          label={`Type ${NEVER_ASK_CONFIRM} to confirm`}
          autoComplete="off"
          spellCheck={false}
          value={neverTyped}
          onChange={(e) => setNeverTyped(e.target.value)}
        />
      </EditorialDialog>
    </div>
  );
}
