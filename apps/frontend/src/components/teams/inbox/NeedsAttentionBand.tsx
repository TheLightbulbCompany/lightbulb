// apps/frontend/src/components/teams/inbox/NeedsAttentionBand.tsx
"use client";

import { useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";
import { FailedRunRow } from "@/components/teams/inbox/FailedRunRow";
import { useInboxTriage } from "@/components/teams/inbox/hooks/useInboxTriage";
import { useAcknowledgedRuns } from "@/components/teams/inbox/hooks/useAcknowledgedRuns";
import { loadBandCollapsed, saveBandCollapsed } from "@/components/teams/shared/lib/inboxStorage";
import type { CompanyAgent, HeartbeatRun } from "@/components/teams/shared/types";

/** One collapsed group of identical failed runs.
 *
 * A flapping heartbeat emits the same failure once per occurrence — on prod
 * a single agent produced 492 identical rows. We collapse by
 * ``(agentId + failureReason)`` (NOT ``run.id`` / ``startedAt``, which are
 * per-occurrence) so the band shows ONE row per distinct failure with a
 * count. ``representative`` is the most-recent member (drives the title +
 * timestamp + View link); ``runs`` is every member so a dismiss can ack
 * them all. */
interface FailedRunGroup {
  key: string;
  representative: HeartbeatRun;
  runs: HeartbeatRun[];
  count: number;
}

/** Most-recent-first comparator on ``startedAt`` (missing sorts last). */
function startedAtDesc(a: HeartbeatRun, b: HeartbeatRun): number {
  const ta = a.startedAt ? Date.parse(a.startedAt) : NaN;
  const tb = b.startedAt ? Date.parse(b.startedAt) : NaN;
  if (Number.isNaN(ta) && Number.isNaN(tb)) return 0;
  if (Number.isNaN(ta)) return 1;
  if (Number.isNaN(tb)) return -1;
  return tb - ta;
}

/** Collapse identical failed runs into groups keyed by agent + reason.
 *
 * Group order follows first-appearance in ``failedRuns`` (the upstream feed
 * is already newest-first); within a group the representative is the
 * most-recent occurrence so its timestamp + run-detail link stay live. */
function groupFailedRuns(failedRuns: HeartbeatRun[]): FailedRunGroup[] {
  const byKey = new Map<string, HeartbeatRun[]>();
  const order: string[] = [];
  for (const run of failedRuns) {
    const key = JSON.stringify([run.agentId ?? "", run.failureReason ?? ""]);
    const bucket = byKey.get(key);
    if (bucket) {
      bucket.push(run);
    } else {
      byKey.set(key, [run]);
      order.push(key);
    }
  }
  return order.map((key) => {
    const runs = byKey.get(key)!;
    const representative = runs.reduce((latest, r) =>
      startedAtDesc(r, latest) < 0 ? r : latest,
    );
    return { key, representative, runs, count: runs.length };
  });
}

export function NeedsAttentionBand({
  companyId,
  agents,
}: {
  companyId: string;
  agents: CompanyAgent[];
}) {
  const { acknowledgedRunIds, ack } = useAcknowledgedRuns(companyId);
  const { failedRuns, count, isLoading, isError } =
    useInboxTriage(acknowledgedRunIds);
  const [collapsed, setCollapsed] = useState<boolean>(() => loadBandCollapsed(companyId));

  // A fetch failure means we can't vouch for "inbox zero", so show a notice
  // rather than silently hiding the band.
  if (isError) {
    return (
      <section
        role="alert"
        className="mb-4 rounded-md border px-3 py-2 text-sm"
        style={{
          borderColor: "color-mix(in srgb, var(--danger) 25%, transparent)",
          background: "color-mix(in srgb, var(--danger) 6%, transparent)",
          color: "var(--danger)",
        }}
      >
        Couldn&apos;t load recent agent failures. Try refreshing.
      </section>
    );
  }

  // First load with nothing cached yet — show a quiet skeleton so the band
  // doesn't pop in after the rest of the inbox renders.
  if (isLoading && count === 0) {
    return (
      <section
        aria-busy="true"
        className="mb-4 rounded-md border"
        style={{ borderColor: "var(--edge)", background: "var(--cream)" }}
      >
        <div className="flex items-center gap-2 px-3 py-2">
          <span
            aria-hidden="true"
            className="block h-3 w-3 animate-pulse rounded bg-[var(--cream-deeper)]"
          />
          <span
            aria-hidden="true"
            className="block h-3 w-32 animate-pulse rounded bg-[var(--cream-deeper)]"
          />
        </div>
      </section>
    );
  }

  // Nothing to triage — render nothing so "inbox zero" stays clean.
  if (count === 0) return null;

  function toggle() {
    const next = !collapsed;
    setCollapsed(next);
    saveBandCollapsed(companyId, next);
  }

  const agentName = (id?: string | null) =>
    agents.find((a) => a.id === id)?.name ?? (id ?? "Agent");

  const failedRunGroups = groupFailedRuns(failedRuns);

  return (
    <section
      className="mb-4 rounded-md border"
      style={{ borderColor: "var(--edge)", background: "var(--cream)" }}
    >
      <button
        type="button"
        onClick={toggle}
        aria-expanded={!collapsed}
        className="flex w-full items-center gap-2 px-3 py-2 text-left"
      >
        {collapsed ? <ChevronRight className="h-4 w-4" /> : <ChevronDown className="h-4 w-4" />}
        <span className="font-medium text-[var(--ink)]">Agent failures</span>
        <span className="text-xs text-[var(--mute)]">{count}</span>
      </button>
      {!collapsed && (
        <ul className="divide-y" style={{ borderColor: "var(--edge)" }}>
          {failedRunGroups.map((g) => (
            <FailedRunRow
              key={g.key || g.representative.id}
              run={g.representative}
              agentName={agentName(g.representative.agentId)}
              count={g.count}
              // Dismissing a grouped row must ack EVERY member id, or the
              // unacked occurrences re-populate the band on the next read.
              onDismiss={() => g.runs.forEach((r) => ack(r.id))}
            />
          ))}
        </ul>
      )}
    </section>
  );
}
