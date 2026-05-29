// apps/frontend/src/components/teams/inbox/ApprovalRow.tsx
"use client";

import { useState } from "react";
import { ShieldQuestion } from "lucide-react";
import { EditorialButton, EditorialInput } from "@/components/teams/shell";
import { timeAgo } from "@/components/teams/shared/lib/timeAgo";
import { useApprovalMutations } from "@/components/teams/inbox/hooks/useApprovalMutations";
import type { Approval } from "@/components/teams/shared/types";

export function ApprovalRow({ approval }: { approval: Approval }) {
  const { approve, reject } = useApprovalMutations();
  const [busy, setBusy] = useState(false);
  const [rejecting, setRejecting] = useState(false);
  const [reason, setReason] = useState("");
  const [error, setError] = useState<string | null>(null);

  const title = approval.title?.trim() ? approval.title : "Approval requested";

  async function run(fn: () => Promise<void>) {
    setBusy(true);
    setError(null);
    try {
      await fn();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Action failed. Try again.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <li className="flex flex-col gap-2 px-3 py-2">
      <div className="flex items-center gap-2">
        <ShieldQuestion className="h-4 w-4 flex-shrink-0" style={{ color: "var(--forest)" }} />
        <span className="flex-1 truncate text-[var(--ink)]">{title}</span>
        {approval.createdAt && (
          <span className="text-xs text-[var(--mute)]">{timeAgo(approval.createdAt)}</span>
        )}
        {!rejecting && (
          <div className="flex gap-2">
            <EditorialButton size="sm" disabled={busy} onClick={() => run(() => approve(approval.id))}>
              Approve
            </EditorialButton>
            <EditorialButton variant="destructive" size="sm" disabled={busy} onClick={() => setRejecting(true)}>
              Reject
            </EditorialButton>
          </div>
        )}
      </div>
      {rejecting && (
        <div className="flex items-end gap-2">
          <EditorialInput
            label="Reason for rejection"
            autoFocus
            placeholder="Reason for rejection"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            disabled={busy}
            className="flex-1"
          />
          <EditorialButton
            variant="destructive"
            size="sm"
            disabled={busy || reason.trim() === ""}
            aria-label="Confirm reject"
            onClick={() => run(async () => { await reject(approval.id, reason.trim()); })}
          >
            Confirm reject
          </EditorialButton>
          <EditorialButton variant="secondary" size="sm" disabled={busy} onClick={() => { setRejecting(false); setReason(""); }}>
            Cancel
          </EditorialButton>
        </div>
      )}
      {error && <span className="text-xs text-[var(--danger)]">{error}</span>}
    </li>
  );
}
