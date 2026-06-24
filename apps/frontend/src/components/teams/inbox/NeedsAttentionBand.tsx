// apps/frontend/src/components/teams/inbox/NeedsAttentionBand.tsx
"use client";

import { useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";
import { ApprovalRow } from "@/components/teams/inbox/ApprovalRow";
import { FailedRunRow } from "@/components/teams/inbox/FailedRunRow";
import { useInboxTriage } from "@/components/teams/inbox/hooks/useInboxTriage";
import { useAcknowledgedRuns } from "@/components/teams/inbox/hooks/useAcknowledgedRuns";
import { loadBandCollapsed, saveBandCollapsed } from "@/components/teams/shared/lib/inboxStorage";
import type { CompanyAgent } from "@/components/teams/shared/types";

export function NeedsAttentionBand({
  companyId,
  agents,
}: {
  companyId: string;
  agents: CompanyAgent[];
}) {
  const { acknowledgedRunIds, ack } = useAcknowledgedRuns(companyId);
  const { approvals, failedRuns, count, isLoading, isError } =
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
        Couldn&apos;t load items that need attention. Try refreshing.
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
        <span className="font-medium text-[var(--ink)]">Needs attention</span>
        <span className="text-xs text-[var(--mute)]">{count}</span>
      </button>
      {!collapsed && (
        <ul className="divide-y" style={{ borderColor: "var(--edge)" }}>
          {approvals.map((a) => (
            <ApprovalRow key={a.id} approval={a} />
          ))}
          {failedRuns.map((r) => (
            <FailedRunRow
              key={r.id}
              run={r}
              agentName={agentName(r.agentId)}
              onDismiss={() => ack(r.id)}
            />
          ))}
        </ul>
      )}
    </section>
  );
}
