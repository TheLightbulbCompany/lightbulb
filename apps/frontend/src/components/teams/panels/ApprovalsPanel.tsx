"use client";

import { useState } from "react";
import { CheckCircle2 } from "lucide-react";
import {
  EditorialButton,
  PanelEmptyState,
  PanelMasthead,
} from "@/components/teams/shell";
import { useTeamsApi } from "@/hooks/useTeamsApi";

interface Approval {
  id: string;
  title: string;
  description?: string;
  createdAt?: string;
}

export function ApprovalsPanel() {
  const { read, post } = useTeamsApi();
  const { data, mutate } = read<{ approvals: Approval[] }>("/approvals");
  const [busy, setBusy] = useState<string | null>(null);

  async function approve(id: string) {
    setBusy(id);
    try {
      // SECURITY: body contains ONLY {note}; the BFF whitelists with
      // extra="forbid" via ApproveApprovalBody (Task 3 schema).
      await post(`/approvals/${id}/approve`, { note: "approved via UI" });
      mutate();
    } finally {
      setBusy(null);
    }
  }

  async function reject(id: string) {
    const reason = prompt("Reason for rejection?");
    if (!reason) return;
    setBusy(id);
    try {
      // SECURITY: body contains ONLY {reason}; the BFF whitelists via
      // RejectApprovalBody (Task 3 schema).
      await post(`/approvals/${id}/reject`, { reason });
      mutate();
    } finally {
      setBusy(null);
    }
  }

  const approvals = data?.approvals ?? [];

  return (
    <div className="editorial-page p-4 sm:p-6 lg:p-8 flex flex-col gap-6 max-w-[1600px] mx-auto w-full">
      <PanelMasthead overline="Workflow" title="Approvals" />
      {approvals.length === 0 ? (
        <PanelEmptyState
          icon={CheckCircle2}
          headline="No pending approvals."
          supporting="When an agent action requires sign-off, it will appear here."
        />
      ) : (
        <ul className="flex flex-col gap-3">
          {approvals.map((a) => (
            <li key={a.id} className="editorial-card p-4">
              <div className="font-medium text-[var(--ink)]">{a.title}</div>
              {a.description && (
                <div className="text-sm text-[var(--ink-soft)] mt-1">
                  {a.description}
                </div>
              )}
              <div className="flex gap-2 mt-3">
                <EditorialButton
                  size="sm"
                  onClick={() => approve(a.id)}
                  disabled={busy === a.id}
                >
                  Approve
                </EditorialButton>
                <EditorialButton
                  variant="destructive"
                  size="sm"
                  onClick={() => reject(a.id)}
                  disabled={busy === a.id}
                >
                  Reject
                </EditorialButton>
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
