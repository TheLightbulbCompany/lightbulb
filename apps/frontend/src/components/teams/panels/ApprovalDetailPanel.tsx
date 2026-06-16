// apps/frontend/src/components/teams/panels/ApprovalDetailPanel.tsx
"use client";

import { useState } from "react";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { EditorialButton } from "@/components/teams/shell";
import { notifyApiError } from "@/lib/notify";
import { useApprovalMutations } from "@/components/teams/inbox/hooks/useApprovalMutations";
import { timeAgo } from "@/components/teams/shared/lib/timeAgo";
import type { ApprovalDetail, CompanyAgent } from "@/components/teams/shared/types";

function approvalLabel(type?: string | null): string {
  return type ? type.replace(/_/g, " ") : "Approval";
}

export function ApprovalDetailPanel({ approvalId }: { approvalId: string }) {
  const { read } = useTeamsApi();
  const { data, isLoading, mutate } = read<ApprovalDetail>(`/approvals/${approvalId}`);
  const { data: agentsData } = read<{ agents: CompanyAgent[] }>("/agents");
  const { approve, reject } = useApprovalMutations();
  const [busy, setBusy] = useState(false);
  const [rejecting, setRejecting] = useState(false);
  const [reason, setReason] = useState("");
  const [showRaw, setShowRaw] = useState(false);

  if (isLoading) return <div className="p-8">Loading…</div>;
  if (!data) return <div className="p-8 text-[var(--mute)]">Approval not found.</div>;

  const agents = agentsData?.agents ?? [];
  const requester = data.requestedByAgentId
    ? agents.find((a) => a.id === data.requestedByAgentId)?.name ?? data.requestedByAgentId
    : data.requestedByUserId ?? "—";
  const isPending = data.status === "pending";

  async function act(fn: () => Promise<void>) {
    setBusy(true);
    try {
      await fn();
      await mutate();
    } catch (e) {
      notifyApiError(e, "Action failed. Try again.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="p-8 max-w-3xl flex flex-col gap-4">
      <div className="flex items-center gap-3">
        <h1 className="text-xl font-semibold capitalize">{approvalLabel(data.type)}</h1>
        <span
          className="text-xs capitalize px-2 py-0.5 rounded"
          style={{ background: "var(--edge)", color: isPending ? "var(--forest)" : "var(--mute)" }}
        >
          {data.status}
        </span>
      </div>
      <div className="text-xs text-[var(--mute)] font-mono">{data.id}</div>
      <div className="text-sm text-[var(--ink)]">
        Requested by <span className="font-medium">{requester}</span>
        {data.createdAt && <> · {timeAgo(data.createdAt)}</>}
      </div>
      {data.decisionNote && (
        <div className="text-sm">
          <span className="text-[var(--mute)]">Decision note: </span>
          {data.decisionNote}
        </div>
      )}
      <div className="flex flex-col gap-1">
        <button
          type="button"
          onClick={() => setShowRaw((v) => !v)}
          className="text-xs self-start"
          style={{ color: "var(--forest)" }}
        >
          {showRaw ? "Hide" : "Show"} request payload
        </button>
        {showRaw && (
          <pre className="bg-[var(--cream-deeper)] border border-[var(--edge)] rounded p-3 text-xs overflow-x-auto whitespace-pre-wrap">
            {JSON.stringify(data.payload ?? {}, null, 2)}
          </pre>
        )}
      </div>
      {isPending && !rejecting && (
        <div className="flex gap-2">
          <EditorialButton size="sm" disabled={busy} onClick={() => act(() => approve(data.id))}>
            Approve
          </EditorialButton>
          <EditorialButton variant="destructive" size="sm" disabled={busy} onClick={() => setRejecting(true)}>
            Reject
          </EditorialButton>
        </div>
      )}
      {isPending && rejecting && (
        <div className="flex items-center gap-2">
          <input
            autoFocus
            aria-label="Reason for rejection"
            placeholder="Reason for rejection"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            disabled={busy}
            className="flex-1 min-w-0 bg-transparent outline-none"
            style={{ borderBottom: "1px solid var(--edge)", color: "var(--ink)" }}
          />
          <EditorialButton
            variant="destructive"
            size="sm"
            disabled={busy || reason.trim() === ""}
            onClick={() => act(async () => { await reject(data.id, reason.trim()); })}
          >
            Confirm reject
          </EditorialButton>
          <EditorialButton variant="secondary" size="sm" disabled={busy} onClick={() => { setRejecting(false); setReason(""); }}>
            Cancel
          </EditorialButton>
        </div>
      )}
    </div>
  );
}
