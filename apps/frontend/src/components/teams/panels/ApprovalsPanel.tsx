"use client";

import { useState } from "react";
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
    <div className="editorial-page p-8 max-w-4xl">
      <h1 className="font-editorial-serif text-4xl mb-6 text-[var(--ink)]">Approvals</h1>
      <ul className="space-y-3">
        {approvals.map((a) => (
          <li key={a.id} className="editorial-card p-4">
            <div className="font-medium text-[var(--ink)]">{a.title}</div>
            {a.description && (
              <div className="text-sm text-[var(--ink-soft)] mt-1">{a.description}</div>
            )}
            <div className="flex gap-2 mt-3">
              <button
                onClick={() => approve(a.id)}
                disabled={busy === a.id}
                className="editorial-btn editorial-btn--primary editorial-btn--sm"
              >
                Approve
              </button>
              <button
                onClick={() => reject(a.id)}
                disabled={busy === a.id}
                className="editorial-btn editorial-btn--secondary editorial-btn--sm"
              >
                Reject
              </button>
            </div>
          </li>
        ))}
        {approvals.length === 0 && (
          <li className="font-editorial-serif text-xl italic text-[var(--mute)]">
            No pending approvals.
          </li>
        )}
      </ul>
    </div>
  );
}
