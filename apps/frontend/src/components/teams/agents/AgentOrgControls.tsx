// apps/frontend/src/components/teams/agents/AgentOrgControls.tsx
"use client";

import { useCallback, useState } from "react";
import type { CompanyAgent } from "@/components/teams/shared/types";
import { EditorialButton } from "@/components/teams/shell";
import { ReportsToPicker } from "@/components/teams/agents/ReportsToPicker";
import { useAgentMutations } from "@/components/teams/agents/hooks/useAgentMutations";

/** Seeded = Lightbulb AI provisioning contract (Friday/Atlas), locked from
 *  deletion/meta-edits. The BFF stamps ``seeded`` from the persisted
 *  seeded-agent IDs (core/services/paperclip_openclaw_pairing.is_seeded_agent),
 *  so a user-created agent using role "ceo" or the name "Atlas" is never
 *  mistaken for one. */
export function isSeededAgent(a: CompanyAgent): boolean {
  return a.seeded === true;
}

/**
 * Org-chart controls for a single agent: the editable reporting line plus the
 * lifecycle actions (pause/resume + two-step delete). Extracted from
 * `AgentManageSection` so the same controls compose into both the `/teams`
 * agent detail view and the `/chat` per-agent inspector's Overview tab — see
 * increment 2 of the unified-nav migration.
 *
 * Writes flow through `useAgentMutations`, which only needs Clerk (no Teams
 * provider) — the reports-to/pause/resume paths hit the BFF and the delete
 * path goes through the global GatewayProvider. The seeded guard (Friday/Atlas
 * are undeletable) and the confirm-before-delete gating live here.
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
  const seeded = isSeededAgent(agent);
  const paused = agent.status === "paused";
  const errored = agent.status === "error";
  const marketplaceLabel = agent.marketplace
    ? `${agent.marketplace.slug} v${agent.marketplace.version} · ${agent.marketplace.seller}`
    : null;

  const run = useCallback(async (fn: () => Promise<void>) => {
    setBusy(true);
    try {
      await fn();
    } finally {
      setBusy(false);
    }
  }, []);

  return (
    <div className="flex flex-col gap-6">
      <section className="editorial-card p-4 flex flex-col gap-1">
        <span className="text-xs uppercase tracking-wide text-[var(--mute)]">Origin</span>
        <span className="text-sm font-medium text-[var(--ink)]">{agent.origin ?? "Custom"}</span>
        {marketplaceLabel ? (
          <span className="text-xs text-[var(--mute)]" title={`Listing ${agent.marketplace?.listingId}`}>
            {marketplaceLabel}
          </span>
        ) : null}
      </section>

      {/* Reporting line — editable here */}
      <section className="editorial-card p-4 flex flex-col gap-2">
        <h3 className="font-editorial-serif text-lg">Reports to</h3>
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
        {!seeded && !confirmDelete ? (
          <EditorialButton
            variant="secondary"
            size="sm"
            disabled={busy}
            onClick={() => setConfirmDelete(true)}
          >
            Delete
          </EditorialButton>
        ) : null}
        {!seeded && confirmDelete ? (
          <>
            <span className="text-sm text-[var(--danger)]">Delete {agent.name}?</span>
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

      {/* Recovery — get a wedged agent unstuck. Available for seeded agents
          too (Friday/Atlas can wedge). "Clear error" only shows on an
          error-status agent (upstream 409s otherwise); "Reset session" clears
          runtime session state, so it's two-step to avoid an accidental wipe.
          Neither dispatches an inference run, so no spend gate applies. */}
      <section className="editorial-card p-4 flex flex-col gap-2">
        <h3 className="font-editorial-serif text-lg">Recovery</h3>
        <p className="text-xs text-[var(--mute)]">
          Get a stuck agent unstuck without deleting it.
        </p>
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
              <span className="text-sm text-[var(--ink-soft)]">
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
