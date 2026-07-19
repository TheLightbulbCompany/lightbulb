"use client";

import { useAuth } from "@clerk/nextjs";
import { useCallback, useEffect, useState } from "react";

import { getCompanyDeploy, rollbackCompanyDeploy } from "@/lib/marketplace/api";
import type { CompanyDeployAgentState, CompanyDeployJob } from "@/lib/marketplace/types";

import { RoutinesEnableCard } from "./RoutinesEnableCard";

// Stop the poll loop after this many *consecutive* failures so a hard-errored
// job (404/403/sustained 5xx) can't silently re-request forever.
const MAX_CONSECUTIVE_POLL_ERRORS = 5;

/** Per-agent state → display glyph. Unknown/interim states render as a spinner
 * so a new backend state can't blank the row. */
function agentGlyph(state: string): { glyph: string; done: boolean; failed: boolean } {
  if (state === "materialized") return { glyph: "✓", done: true, failed: false };
  if (state === "failed") return { glyph: "✕", done: false, failed: true };
  return { glyph: "…", done: false, failed: false };
}

/**
 * Live progress for one company deploy.
 *
 * Owns the poll loop: given `jobId` it polls the job row every ~2s (recursive
 * `setTimeout`, not `setInterval`, so ticks never overlap and the loop simply
 * stops rescheduling once the job goes terminal — no interval to leak). The
 * effect clears any pending tick on unmount and guards every post-await
 * `setState` behind a `cancelled` flag. Poll failures are bounded: a
 * non-transient 4xx stops immediately, and any run of
 * `MAX_CONSECUTIVE_POLL_ERRORS` failures surfaces a "lost contact" state
 * instead of re-requesting forever. The happy-path 2s cadence is unchanged.
 *
 * Terminal handling:
 *  - `completed`   → render the RoutinesEnableCard.
 *  - `failed`      → show the error + a single Roll back action (tear down the
 *                    partially-created agents). There is deliberately NO
 *                    "resume": re-POSTing `/deploy` mints a *new* job and
 *                    re-creates every agent the failed job already made (a full
 *                    double-create). A proper resume endpoint that reuses the
 *                    existing job's partial agents is a tracked follow-up; until
 *                    then the buyer rolls back, then re-deploys from the top of
 *                    the page.
 *  - `rolled_back` → confirmation copy. The rollback POST returns 202 with the
 *                    teardown dispatched server-side; rather than re-open the
 *                    (already-stopped) poll loop to chase the async flip, we
 *                    trust the 202 and show the rolled-back state optimistically.
 */
export function DeployProgress({ jobId }: { jobId: string }) {
  const { getToken } = useAuth();
  const [job, setJob] = useState<CompanyDeployJob | null>(null);
  const [rolledBack, setRolledBack] = useState(false);
  const [pollError, setPollError] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let consecutiveErrors = 0;

    async function tick() {
      if (cancelled) return;
      try {
        const jwt = await getToken();
        if (!jwt) throw new Error("no auth token");
        const next = await getCompanyDeploy({ jobId, jwt });
        if (cancelled) return;
        consecutiveErrors = 0;
        setJob(next);
        // Reschedule only while running — terminal states stop the loop.
        if (next.status === "running") {
          timer = setTimeout(tick, 2000);
        }
      } catch (e) {
        if (cancelled) return;
        consecutiveErrors += 1;
        // A 4xx is non-transient (job gone / not ours) — stop immediately.
        // Otherwise stop once a run of failures crosses the cap.
        const status = (e as { status?: number }).status;
        const clientError = typeof status === "number" && status >= 400 && status < 500;
        if (clientError || consecutiveErrors >= MAX_CONSECUTIVE_POLL_ERRORS) {
          setPollError(true);
          return;
        }
        timer = setTimeout(tick, 2000);
      }
    }

    tick();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [jobId, getToken]);

  const rollback = useCallback(async () => {
    setActionError(null);
    setBusy(true);
    try {
      const jwt = await getToken();
      if (!jwt) throw new Error("Not signed in.");
      await rollbackCompanyDeploy({ jobId, jwt });
      setRolledBack(true);
    } catch (e) {
      setActionError(e instanceof Error ? e.message : "Rollback failed.");
    } finally {
      setBusy(false);
    }
  }, [getToken, jobId]);

  if (!job) {
    return pollError ? (
      <p className="text-sm text-[var(--danger)]" role="alert">
        Lost contact with the deploy — reload to check status.
      </p>
    ) : (
      <p className="text-sm text-[var(--mute)]" role="status">
        Starting deploy…
      </p>
    );
  }

  // A successful rollback POST (202) overrides the still-"failed" job row.
  const displayStatus = rolledBack ? "rolled_back" : job.status;

  return (
    <div>
      <AgentTicks agents={job.agents} />

      {pollError && (
        <p className="mt-4 text-sm text-[var(--danger)]" role="alert">
          Lost contact with the deploy — reload to check status.
        </p>
      )}

      {!pollError && displayStatus === "running" && (
        <p className="mt-4 text-sm text-[var(--mute)]" role="status">
          Deploying your company…
        </p>
      )}

      {displayStatus === "failed" && (
        <div className="mt-4">
          <p
            className="rounded-lg border border-[rgba(179,36,36,0.2)] bg-[rgba(179,36,36,0.06)] px-3 py-2 text-sm text-[var(--danger)]"
            role="alert"
          >
            {job.error || "Deploy failed."}
          </p>
          <p className="mt-3 text-sm leading-6 text-[var(--ink-soft)]">
            This deploy failed partway through. Roll it back to remove the agents
            it created, then deploy again from the top of the page.
          </p>
          <div className="mt-3 flex gap-3">
            <button
              type="button"
              onClick={rollback}
              disabled={busy}
              className="editorial-btn editorial-btn--primary h-10 px-4 text-sm disabled:opacity-50"
            >
              {busy ? "Working…" : "Roll back this deploy"}
            </button>
          </div>
          {actionError && (
            <p className="mt-2 text-sm text-[var(--danger)]" role="alert">
              {actionError}
            </p>
          )}
        </div>
      )}

      {displayStatus === "rolled_back" && (
        <p className="mt-4 text-sm text-[var(--ink-soft)]" role="status">
          This deploy was rolled back — the agents it created are being removed.
          You can deploy again from the top of the page.
        </p>
      )}

      {displayStatus === "completed" && <RoutinesEnableCard routineIds={job.routine_ids} />}
    </div>
  );
}

function AgentTicks({ agents }: { agents: CompanyDeployAgentState[] }) {
  if (agents.length === 0) return null;
  return (
    <ul className="flex flex-col gap-2" data-testid="agent-ticks">
      {agents.map((agent) => {
        const { glyph, done, failed } = agentGlyph(agent.state);
        const tone = failed
          ? "text-[var(--danger)]"
          : done
            ? "text-[var(--forest)]"
            : "text-[var(--mute)]";
        return (
          <li key={agent.slug} className="flex items-center gap-3">
            <span className={`font-mono text-sm ${tone}`} aria-hidden="true">
              {glyph}
            </span>
            <span className="font-mono text-xs text-[var(--ink-soft)]">{agent.slug}</span>
            <span className="font-mono text-[10px] uppercase tracking-[0.12em] text-[var(--mute)]">
              {agent.state}
            </span>
          </li>
        );
      })}
    </ul>
  );
}
