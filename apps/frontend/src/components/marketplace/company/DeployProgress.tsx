"use client";

import { useAuth } from "@clerk/nextjs";
import { useCallback, useEffect, useState } from "react";

import {
  deployCompany,
  getCompanyDeploy,
  rollbackCompanyDeploy,
} from "@/lib/marketplace/api";
import type { CompanyDeployAgentState, CompanyDeployJob } from "@/lib/marketplace/types";

import { RoutinesEnableCard } from "./RoutinesEnableCard";

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
 * Owns the poll loop: given an initial `jobId` it polls the job row every ~2s
 * (recursive `setTimeout`, not `setInterval`, so ticks never overlap and the
 * loop simply stops rescheduling once the job goes terminal — no interval to
 * leak). The effect also clears any pending tick on unmount and guards every
 * post-await `setState` behind a `cancelled` flag.
 *
 * Terminal handling:
 *  - `completed`   → render the RoutinesEnableCard.
 *  - `failed`      → show the error + Resume (re-POST the deploy → new job) and
 *                    Roll back (tear down what was created) actions.
 *  - `rolled_back` → confirmation copy. The rollback POST returns 202 with the
 *                    teardown dispatched server-side; rather than re-open the
 *                    (already-stopped) poll loop to chase the async flip, we
 *                    trust the 202 and show the rolled-back state optimistically.
 */
export function DeployProgress({
  jobId,
  listingId,
}: {
  jobId: string;
  listingId: string;
}) {
  const { getToken } = useAuth();
  const [currentJobId, setCurrentJobId] = useState(jobId);
  const [job, setJob] = useState<CompanyDeployJob | null>(null);
  const [rolledBack, setRolledBack] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;

    async function tick() {
      if (cancelled) return;
      try {
        const jwt = await getToken();
        if (!jwt || cancelled) return;
        const next = await getCompanyDeploy({ jobId: currentJobId, jwt });
        if (cancelled) return;
        setJob(next);
        // Reschedule only while running — terminal states stop the loop.
        if (next.status === "running") {
          timer = setTimeout(tick, 2000);
        }
      } catch {
        // Transient (token refresh, 5xx, network) — keep polling.
        if (!cancelled) timer = setTimeout(tick, 2000);
      }
    }

    tick();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [currentJobId, getToken]);

  const resume = useCallback(async () => {
    setActionError(null);
    setBusy(true);
    try {
      const jwt = await getToken();
      if (!jwt) throw new Error("Not signed in.");
      const { job_id } = await deployCompany({ listingId, jwt });
      setJob(null);
      setRolledBack(false);
      setCurrentJobId(job_id); // new id re-runs the poll effect
    } catch (e) {
      setActionError(e instanceof Error ? e.message : "Resume failed.");
    } finally {
      setBusy(false);
    }
  }, [getToken, listingId]);

  const rollback = useCallback(async () => {
    setActionError(null);
    setBusy(true);
    try {
      const jwt = await getToken();
      if (!jwt) throw new Error("Not signed in.");
      await rollbackCompanyDeploy({ jobId: currentJobId, jwt });
      setRolledBack(true);
    } catch (e) {
      setActionError(e instanceof Error ? e.message : "Rollback failed.");
    } finally {
      setBusy(false);
    }
  }, [getToken, currentJobId]);

  if (!job) {
    return (
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

      {displayStatus === "running" && (
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
          <div className="mt-3 flex gap-3">
            <button
              type="button"
              onClick={resume}
              disabled={busy}
              className="editorial-btn editorial-btn--primary h-10 px-4 text-sm disabled:opacity-50"
            >
              {busy ? "Working…" : "Resume"}
            </button>
            <button
              type="button"
              onClick={rollback}
              disabled={busy}
              className="editorial-btn h-10 px-4 text-sm disabled:opacity-50"
            >
              {busy ? "Working…" : "Roll back"}
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
