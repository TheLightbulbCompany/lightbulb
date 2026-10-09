// apps/frontend/src/components/teams/agents/AgentOrgControls.tsx
"use client";

import { useCallback, useState } from "react";
import type { CompanyAgent } from "@/components/teams/shared/types";
import { EditorialButton } from "@/components/teams/shell";
import { ReportsToPicker } from "@/components/teams/agents/ReportsToPicker";
import { useAgentMutations } from "@/components/teams/agents/hooks/useAgentMutations";
import { classifyAgentStatus } from "@/components/teams/shared/lib/agentStatus";
import { notifyApiError } from "@/lib/notify";

/**
 * Org-chart controls for a single agent: the editable reporting line plus the
 * lifecycle actions (pause/resume + two-step delete). Extracted from
 * `AgentManageSection` so the same controls compose into both the `/teams`
 * agent detail view and the `/chat` per-agent inspector's Overview tab — see
 * increment 2 of the unified-nav migration.
 *
 * Writes flow through `useAgentMutations`, which only needs Clerk (no Teams
 * provider) — the reports-to/pause/resume paths hit the BFF and the delete
 * path goes through the global GatewayProvider. The backend enforces graph
 * invariants; this surface only owns confirm-before-delete gating.
 */
export function AgentOrgControls({
  agent,
  agents,
  onDeleted,
}: {
  agent: CompanyAgent;
  agents: CompanyAgent[];
  /** Called after a successful delete so the host view can navigate away —
   *  remove() clears the agent's detail cache. */
  onDeleted?: () => void;
}) {
  const { setReportsTo, pause, resume, resetSession, clearError, remove } =
    useAgentMutations();
  const [busy, setBusy] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [confirmReset, setConfirmReset] = useState(false);
  const paused = agent.status === "paused";
  const errored = agent.status === "error";
  // `status` is a STORED field on the Paperclip agent row, forwarded verbatim
  // by the BFF — not derived from the run ledger. So a run that died without
  // writing its terminal status parks the agent on a `live` value forever, and
  // every surface keyed off it (roster rail + word, org-chart dot, the chat
  // avatar's "Agent working" halo) keeps saying the agent is busy with nothing
  // running. The owner's only clue today is those indicators, which is exactly
  // the thing they already distrust; showing the raw value here makes the
  // stale field itself visible and reportable.
  const live = classifyAgentStatus(agent.status) === "live";
  const marketplaceLabel = agent.marketplace
    ? `${agent.marketplace.slug} v${agent.marketplace.version} · ${agent.marketplace.seller}`
    : null;

  const run = useCallback(async (fn: () => Promise<void>) => {
    setBusy(true);
    try {
      await fn();
    } catch (err) {
      notifyApiError(err, "Couldn't update the agent");
    } finally {
      setBusy(false);
    }
  }, []);

  return (
    <div className="flex flex-col gap-6">
      <section className="editorial-card p-4 flex flex-col gap-1">
        <span className="label-caps text-[var(--midnight-muted)]">Origin</span>
        <span className="text-sm font-medium text-[var(--midnight)]">{agent.origin ?? "Custom"}</span>
        {marketplaceLabel ? (
          <span className="text-xs text-[var(--midnight-muted)]" title={`Listing ${agent.marketplace?.listingId}`}>
            {marketplaceLabel}
          </span>
        ) : null}
      </section>

      {/* Reporting line — editable here */}
      <section className="editorial-card p-4 flex flex-col gap-2">
        <h3 className="font-editorial-serif text-base">Reports to</h3>
        <ReportsToPicker
          agents={agents}
          agentId={agent.id}
          value={agent.reportsTo}
          disabled={busy}
          onChange={(managerId) => run(() => setReportsTo(agent.id, managerId))}
        />
      </section>

      {/* Lifecycle actions */}
      <section className="flex items-center gap-2">
        {paused ? (
          <EditorialButton
            variant="secondary"
            size="sm"
            disabled={busy}
            onClick={() => run(() => resume(agent.id))}
          >
            Resume
          </EditorialButton>
        ) : (
          <EditorialButton
            variant="secondary"
            size="sm"
            disabled={busy}
            onClick={() => run(() => pause(agent.id))}
          >
            Pause
          </EditorialButton>
        )}
        {!confirmDelete ? (
          <EditorialButton
            variant="secondary"
            size="sm"
            disabled={busy}
            onClick={() => setConfirmDelete(true)}
          >
            Delete
          </EditorialButton>
        ) : null}
        {confirmDelete ? (
          <>
            <span className="text-sm text-[var(--err)]">Delete {agent.name}?</span>
            <EditorialButton
              size="sm"
              disabled={busy}
              onClick={() =>
                run(async () => {
                  await remove(agent.id);
                  onDeleted?.();
                })
              }
            >
              Confirm
            </EditorialButton>
            <EditorialButton
              variant="secondary"
              size="sm"
              disabled={busy}
              onClick={() => setConfirmDelete(false)}
            >
              Cancel
            </EditorialButton>
          </>
        ) : null}
      </section>

      {/* Recovery — get a wedged agent unstuck. Available for starter agents
          too. "Clear error" only shows on an
          error-status agent (upstream 409s otherwise); "Reset session" clears
          runtime session state, so it's two-step to avoid an accidental wipe.
          Neither dispatches an inference run, so no spend gate applies. */}
      <section className="editorial-card p-4 flex flex-col gap-2">
        <h3 className="font-editorial-serif text-base">Recovery</h3>
        <p className="text-xs text-[var(--midnight-muted)]">
          Get a stuck agent unstuck without deleting it.
        </p>
        {agent.status ? (
          <p className="text-xs text-[var(--midnight-muted)]">
            Reported status: <span className="font-mono">{agent.status}</span>
          </p>
        ) : null}
        {live ? (
          <p className="text-xs text-[var(--midnight-soft)]">
            {agent.name} is shown as working everywhere it appears. If no run is actually in
            flight, that is a stale status on the agent record rather than real work. Reset
            session clears {agent.name}&apos;s runtime session state without starting a run or
            spending anything.
          </p>
        ) : null}
        <div className="flex flex-wrap items-center gap-2">
          {errored ? (
            <EditorialButton
              variant="secondary"
              size="sm"
              disabled={busy}
              onClick={() => run(() => clearError(agent.id))}
            >
              Clear error
            </EditorialButton>
          ) : null}
          {!confirmReset ? (
            <EditorialButton
              variant="secondary"
              size="sm"
              disabled={busy}
              onClick={() => setConfirmReset(true)}
            >
              Reset session
            </EditorialButton>
          ) : (
            <>
              <span className="text-sm text-[var(--midnight-soft)]">
                Reset {agent.name}&apos;s session?
              </span>
              <EditorialButton
                size="sm"
                disabled={busy}
                onClick={() =>
                  run(async () => {
                    await resetSession(agent.id);
                    setConfirmReset(false);
                  })
                }
              >
                Confirm
              </EditorialButton>
              <EditorialButton
                variant="secondary"
                size="sm"
                disabled={busy}
                onClick={() => setConfirmReset(false)}
              >
                Cancel
              </EditorialButton>
            </>
          )}
        </div>
      </section>
    </div>
  );
}
