// apps/frontend/src/components/teams/inbox/ApprovalRow.tsx
"use client";

import { useState } from "react";
import { ShieldQuestion } from "lucide-react";
import { EditorialButton } from "@/components/teams/shell";
import { notifyApiError } from "@/lib/notify";
import { timeAgo } from "@/components/teams/shared/lib/timeAgo";
import { useApprovalMutations } from "@/components/teams/inbox/hooks/useApprovalMutations";
import { approvalLabel } from "@/components/teams/inbox/lib/approvalLabel";
import type { Approval } from "@/components/teams/shared/types";

export function ApprovalRow({ approval }: { approval: Approval }) {
  const { approve, reject } = useApprovalMutations();
  const [busy, setBusy] = useState(false);
  const [rejecting, setRejecting] = useState(false);
  const [reason, setReason] = useState("");

  // Upstream never sends a top-level `title`, so the old `approval.title`
  // read always fell back to a generic "Approval requested". Synthesize a
  // contextual label from `type` + `payload` (ported from upstream's
  // `approvalLabel`), e.g. "Hire Agent: Designer".
  const title = approvalLabel(approval.type, approval.payload);
  // Both `pending` and `revision_requested` are actionable — the reviewer can
  // still approve/reject. (Only terminal states hide the action buttons.)
  const isActionable =
    approval.status === "pending" || approval.status === "revision_requested";

  async function run(fn: () => Promise<void>) {
    setBusy(true);
    try {
      await fn();
    } catch (e) {
      notifyApiError(e, "Action failed. Try again.");
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
        {isActionable && !rejecting && (
          <div className="flex gap-2">
            <EditorialButton size="sm" disabled={busy} onClick={() => run(() => approve(approval.id))}>
              Approve
            </EditorialButton>
            <EditorialButton variant="destructive" size="sm" disabled={busy} onClick={() => setRejecting(true)}>
              Reject
            </EditorialButton>
          </div>
        )}
        {!isActionable && (
          <span className="text-xs capitalize text-[var(--mute)]">{approval.status}</span>
        )}
      </div>
      {isActionable && rejecting && (
        <div className="flex items-center gap-2">
          <input
            autoFocus
            aria-label="Reason for rejection"
            placeholder="Reason for rejection"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Escape") {
                e.preventDefault();
                setRejecting(false);
                setReason("");
              } else if (e.key === "Enter" && reason.trim() !== "") {
                e.preventDefault();
                void run(async () => { await reject(approval.id, reason.trim()); });
              }
            }}
            disabled={busy}
            className="flex-1 min-w-0 bg-transparent outline-none"
            style={{
              borderBottom: "1px solid var(--edge)",
              color: "var(--ink)",
            }}
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
    </li>
  );
}
