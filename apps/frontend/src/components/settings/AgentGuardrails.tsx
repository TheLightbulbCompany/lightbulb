"use client";

// Settings › Approvals (UX audit slice 2): how agents ask, what they may do
// without asking (the Always-allow rules a decision card wrote), and the kill
// switch. It used to sit at the foot of Connections, under ~20 integrations.
//
// The approval mode (founder ruling 6 Nov 2026) replaced the four "Ask me
// first" switches: one choice per workspace, read on the server by the effects
// route (`runtime_internal.effect_check`), so an agent can never change it.
// The old keys stay stored: `outreach`/`money_moves` still pick who answers a
// card. Never ask is admin-only behind a typed word, checked on both sides.

import { useState } from "react";
import { ApiError, effectName } from "@lightbulb/shared";
import useSWR, { useSWRConfig } from "swr";
import { ShieldAlert } from "lucide-react";

import { PanelSection, EditorialDialog, EditorialButton, EditorialInput } from "@/components/teams/shell";
import { useIsWorkspaceAdmin } from "@/hooks/useIsWorkspaceAdmin";
import { useApi } from "@/lib/api";
import { notifyApiError, notifyError, notifySuccess } from "@/lib/notify";

const POLICY_PATH = "/company/policy";
const RULES_PATH = "/company/policy/always-allow";

/** One Always-allow rule (questions.always_allow_rules): an action kind and the one target it is allowed for. */
type AlwaysRule = { id: string; kind: string; key: string; value: string };

/** "Email · priya@acme.com": the action in words (the card's own names) and its one target. */
const ruleLine = (rule: AlwaysRule) => `${effectName({ kind: rule.kind, tool: rule.kind })} · ${rule.value}`;

/** Settings › Approvals, its own page under Members. */
export function ApprovalsPanel() {
  return (
    <div className="settings-panel" id="panel-approvals" role="tabpanel">
      <h1 className="settings-page-title">Approvals</h1>
      <p className="settings-page-desc">When agents stop for your yes, and what they may do without asking.</p>
      <AgentGuardrails />
    </div>
  );
}

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
  const [rulesOpen, setRulesOpen] = useState(true);
  const [dangerOpen, setDangerOpen] = useState(true);
  const [removing, setRemoving] = useState<string | null>(null);
  const [revokeOpen, setRevokeOpen] = useState(false);
  const [typed, setTyped] = useState("");
  const [neverOpen, setNeverOpen] = useState(false);
  const [neverTyped, setNeverTyped] = useState("");
  const [busy, setBusy] = useState(false);

  const policy = useSWR<CapabilityPolicy>(
    POLICY_PATH,
    (p: string) => api.get(p) as Promise<CapabilityPolicy>,
  );

  const rules = useSWR<{ rules: AlwaysRule[] }>(
    canManage ? RULES_PATH : null,
    (p: string) => api.get(p) as Promise<{ rules: AlwaysRule[] }>,
  );

  async function removeRule(rule: AlwaysRule) {
    setRemoving(rule.id);
    try {
      await api.del(`${RULES_PATH}/${encodeURIComponent(rule.id)}`);
    } catch (e) {
      // Already gone (404): the re-read below drops it all the same.
      if (!(e instanceof ApiError && e.status === 404)) {
        notifyApiError(e, "Couldn't remove that rule. Try again.");
      }
    } finally {
      await rules.mutate();
      setRemoving(null);
    }
  }

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
        label="How agents ask"
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
                // Option rows, not bare browser radios (W14): the input keeps keyboard and screen-reader behaviour, drawn as ours.
                <label key={m.value} className="flex cursor-pointer items-start gap-3 rounded-lg border border-[var(--edge)] px-3 py-2.5 transition-colors duration-(--dur-fast) hover:bg-[var(--muted)] has-[:checked]:border-[var(--foreground)] has-[:focus-visible]:outline-2 has-[:focus-visible]:outline-[var(--ring)] has-[:disabled]:cursor-default has-[:disabled]:hover:bg-transparent">
                  <input
                    type="radio"
                    name="approval-mode"
                    value={m.value}
                    checked={policyMap.approval_mode === m.value}
                    onChange={() => void setMode(m.value)}
                    className="mt-[3px] size-4 shrink-0 appearance-none rounded-full border border-[var(--muted-foreground)] bg-[var(--background)] outline-none transition-[border-width] duration-(--dur-fast) checked:border-[5px] checked:border-[var(--foreground)]"
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
                Anyone who answers a question in a thread can choose Always allow for that recipient, app or repo. Those rules are listed below.
              </p>
            </fieldset>
          )}

          {!canManage && (
            <p className="text-caption" style={{ color: "var(--midnight-muted)" }}>
              A workspace admin changes this.
            </p>
          )}

        </div>
      </PanelSection>

      {canManage && (
        <PanelSection label="Always allowed" count={rules.data?.rules.length} open={rulesOpen} onOpenChange={setRulesOpen}>
          <div className="py-3">
            {rules.error && !rules.data && (
              <p className="text-ui" style={{ color: "var(--err)" }}>
                Couldn&apos;t load the rules. Refresh to try again.
              </p>
            )}
            {rules.data && rules.data.rules.length === 0 && (
              <p className="text-caption" style={{ color: "var(--midnight-muted)" }}>
                Nothing yet. Press Always allow on a decision card and the rule shows here.
              </p>
            )}
            {rules.data && rules.data.rules.length > 0 && (
              <ul className="flex flex-col" aria-label="Always allowed">
                {rules.data.rules.map((rule) => (
                  <li key={rule.id} className="flex items-center gap-3 border-b py-2 last:border-b-0" style={{ borderColor: "var(--edge)" }}>
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-ui" style={{ color: "var(--midnight)" }}>
                        {ruleLine(rule)}
                      </span>
                      <span className="block text-caption" style={{ color: "var(--midnight-muted)" }}>
                        Any specialist, without asking
                      </span>
                    </span>
                    <EditorialButton
                      variant="secondary"
                      size="sm"
                      disabled={removing !== null}
                      aria-label={`Remove ${ruleLine(rule)}`}
                      onClick={() => void removeRule(rule)}
                    >
                      {removing === rule.id ? "Removing…" : "Remove"}
                    </EditorialButton>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </PanelSection>
      )}

      {canManage && (
        <PanelSection label="Danger zone" open={dangerOpen} onOpenChange={setDangerOpen}>
          <div className="flex flex-col gap-2 py-3">
            <p className="text-ui" style={{ color: "var(--midnight)" }}>
              Revoke everything
            </p>
            <p className="text-caption" style={{ color: "var(--midnight-muted)" }}>
              Cancels open provider setups, deletes our copies of your credentials, and disconnects
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
        </PanelSection>
      )}

      <EditorialDialog
        open={revokeOpen}
        onOpenChange={(next) => {
          if (!next && !busy) {
            setRevokeOpen(false);
            setTyped("");
          }
        }}
        title="Revoke everything?"
        description="This cancels open provider setups, deletes our copies of your credentials, and disconnects your connected apps and Stripe. It does not cancel or delete anything you already own at those providers."
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
