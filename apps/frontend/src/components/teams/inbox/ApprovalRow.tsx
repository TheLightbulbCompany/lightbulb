// apps/frontend/src/components/teams/inbox/ApprovalRow.tsx
"use client";

import { useState } from "react";
import { ShieldQuestion } from "lucide-react";
import { EditorialButton } from "@/components/teams/shell";
import { notifyApiError, notifyError } from "@/lib/notify";
import { useApi } from "@/lib/api";
import { closePendingTab, navigatePendingTab, openPendingTab } from "@/components/settings/openInNewTab";
import { timeAgo } from "@/components/teams/shared/lib/timeAgo";
import { useApprovalMutations } from "@/components/teams/inbox/hooks/useApprovalMutations";
import { approvalLabel, isCompanyStripeApproval } from "@/components/teams/inbox/lib/approvalLabel";
import type { Approval } from "@/components/teams/shared/types";

const POPUP_BLOCKED_MSG =
  "Your browser blocked the Stripe window — allow popups for this site and try again.";

export function ApprovalRow({ approval }: { approval: Approval }) {
  const { approve, reject } = useApprovalMutations();
  const api = useApi();
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

  /**
   * Open the tab SYNCHRONOUSLY (the click is the only moment the browser
   * lets us), point it at the minted URL, and only then approve the card —
   * a `window.open` after the `await` is blocked by Safari, and approving a
   * hand-off the owner never saw is the failure that matters here.
   */
  function openThenApprove(fetchUrl: () => Promise<string>) {
    const w = openPendingTab();
    if (!w) {
      notifyError(POPUP_BLOCKED_MSG);
      return;
    }
    void run(async () => {
      let url: string;
      try {
        url = await fetchUrl();
      } catch (e) {
        closePendingTab(w);
        throw e;
      }
      navigatePendingTab(w, url);
      await approve(approval.id);
    });
  }

  return (
    <li className="flex flex-col gap-2 px-3 py-2">
      <div className="flex items-center gap-2">
        <ShieldQuestion size={16} className="shrink-0" style={{ color: "var(--midnight)" }} />
        <span className="min-w-0 flex-1 truncate text-ui text-[var(--midnight)]">{title}</span>
        {approval.createdAt && (
          <span className="shrink-0 whitespace-nowrap text-caption text-[var(--midnight-muted)]">{timeAgo(approval.createdAt)}</span>
        )}
        {isActionable && !rejecting && (
          <div className="flex shrink-0 gap-2">
            {isCompanyStripeApproval(approval) ? (
              <>
                <EditorialButton
                  size="sm"
                  disabled={busy}
                  onClick={() =>
                    openThenApprove(
                      async () =>
                        ((await api.post("/company/stripe/create", {})) as { onboarding_url: string })
                          .onboarding_url,
                    )
                  }
                >
                  Set up Stripe
                </EditorialButton>
                <EditorialButton
                  size="sm"
                  disabled={busy}
                  onClick={() =>
                    openThenApprove(
                      async () =>
                        ((await api.get("/company/stripe/connect/start")) as { authorize_url: string })
                          .authorize_url,
                    )
                  }
                >
                  Connect existing
                </EditorialButton>
              </>
            ) : (
              <EditorialButton size="sm" disabled={busy} onClick={() => run(() => approve(approval.id))}>
                Approve
              </EditorialButton>
            )}
            <EditorialButton variant="destructive" size="sm" disabled={busy} onClick={() => setRejecting(true)}>
              Reject
            </EditorialButton>
          </div>
        )}
        {!isActionable && (
          <span className="shrink-0 whitespace-nowrap text-caption capitalize text-[var(--midnight-muted)]">{approval.status}</span>
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
            className="flex-1 min-w-0 bg-transparent"
            style={{
              borderBottom: "1px solid var(--edge)",
              color: "var(--midnight)",
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
