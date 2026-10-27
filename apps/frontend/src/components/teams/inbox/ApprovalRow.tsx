// apps/frontend/src/components/teams/inbox/ApprovalRow.tsx
"use client";

import { useState } from "react";
import { ShieldQuestion } from "lucide-react";
import { EditorialButton } from "@/components/teams/shell";
import { notifyApiError, notifyError } from "@/lib/notify";
import { useApi } from "@/lib/api";
import { closePendingTab, navigatePendingTab, openPendingTab } from "@/components/settings/openInNewTab";
import { isTauri, openExternalUrl } from "@/lib/externalAuth";
import { timeAgo } from "@/components/teams/shared/lib/timeAgo";
import { handleShellLinkClick, navigateShell, teamsDetailUrl, teamsPanelUrl } from "@/lib/shellNav";
import { useCompanyStripeStatus } from "@/components/settings/CompanyStripeSection";
import { useApprovalMutations } from "@/components/teams/inbox/hooks/useApprovalMutations";
import {
  approvalLabel,
  isCompanyStripeApproval,
  isPaymentSetupApproval,
  isProviderConnectApproval,
  isProviderConsentApproval,
  isProviderInformationApproval,
  isPurchaseRequestApproval,
  payloadString,
} from "@/components/teams/inbox/lib/approvalLabel";
import {
  ProvisioningConsentActions,
  formatUsageLimit,
  usageLimitOf,
} from "@/components/teams/inbox/ProvisioningConsentActions";
import { ProvisioningInformationForm } from "@/components/teams/inbox/ProvisioningInformationForm";
import { AskSomeoneToDecide } from "@/components/teams/inbox/AskSomeoneToDecide";
import type { Approval } from "@/components/teams/shared/types";

const POPUP_BLOCKED_MSG =
  "Your browser blocked the new tab — allow popups for this site and try again.";
const NO_URL_MSG =
  "There is nothing to open for this step right now — approve the card and it will carry on.";

// Every payload on this rail is agent-authored, so a link off one is
// untrusted: it only ever renders when it's an https:// URL.

export function ApprovalRow({
  approval,
  showDetailLink = true,
}: {
  approval: Approval;
  /** Off when the row is rendered INSIDE the approval detail it would link to. */
  showDetailLink?: boolean;
}) {
  const { approve, reject } = useApprovalMutations();
  const api = useApi();
  // "Set up Stripe" / "Connect existing" are the Connect doors, which exist
  // only while the provisioning rail is on (same gate CompanyStripeSection
  // uses). While it is off the card sends the owner to Accounts, where the
  // API-key path lives.
  const { data: stripeStatus } = useCompanyStripeStatus();
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
   * Open the tab SYNCHRONOUSLY (the click is the only moment the browser lets
   * us), point it at the URL minted for THIS click, and — only where the
   * hand-off IS the decision (P0a's Stripe card) — approve after. A
   * `window.open` after the `await` is blocked by Safari, and approving a
   * hand-off the owner never saw is the failure that matters here.
   *
   * On the provisioning rail `thenApprove` is false: opening a provider's
   * OAuth page or a checkout is not consent, so Approve stays a second click.
   */
  function openMintedUrl(fetchUrl: () => Promise<string | null | undefined>, thenApprove: boolean) {
    // `disabled={busy}` only takes effect on the next render, so two clicks in
    // one frame both get through it. Each one mints a checkout session or an
    // account request, so the guard is here too, on the value itself.
    if (busy) return;
    // Desktop (Tauri): WKWebView blocks window.open, so the two-phase tab
    // dance would always read as "popup blocked". The shell plugin has no
    // user-activation constraint, so mint the URL first and open it in the
    // SYSTEM browser — the webview must never navigate to a provider's
    // authorize page (embedded-webview blocks + a callback the webview never
    // sees). The open is AWAITED and throws on failure, so a hand-off the
    // owner never saw is never approved — run()'s catch surfaces the error.
    if (isTauri()) {
      void run(async () => {
        const url = await fetchUrl();
        if (!url) {
          notifyError(NO_URL_MSG);
          return;
        }
        await openExternalUrl(url);
        if (thenApprove) await approve(approval.id);
      });
      return;
    }
    const w = openPendingTab();
    if (!w) {
      notifyError(POPUP_BLOCKED_MSG);
      return;
    }
    void run(async () => {
      let url: string | null | undefined;
      try {
        url = await fetchUrl();
      } catch (e) {
        closePendingTab(w);
        throw e;
      }
      if (!url) {
        closePendingTab(w);
        notifyError(NO_URL_MSG);
        return;
      }
      navigatePendingTab(w, url);
      if (thenApprove) await approve(approval.id);
    });
  }

  /**
   * Reject: the workflow hears first, so it stops instead of polling for a
   * week.
   *
   * WHICH call depends on where the row is. `/reject` only accepts
   * `awaiting_approval`, which is the consent card and nothing else — the
   * connect, information and payment rows are past it and would 409. Those
   * withdraw instead, and a withdrawal our side refuses (something has already
   * been created on the owner's Stripe account) still clears the card: the
   * owner asked to be rid of it, and the resource is theirs to remove.
   */
  async function rejectWithReason() {
    const workflowId = payloadString(approval.payload, "workflow_id");
    if (workflowId && isProviderConsentApproval(approval)) {
      await api.post(`/company/provisioning/workflows/${workflowId}/reject`, {});
    } else if (
      workflowId &&
      (isProviderConnectApproval(approval) ||
        isProviderInformationApproval(approval) ||
        isPaymentSetupApproval(approval))
    ) {
      try {
        await api.post(`/company/provisioning/workflows/${workflowId}/cancel`, {});
      } catch (e) {
        const status = (e as { status?: number }).status;
        if (status !== 409 && status !== 404) throw e;
        notifyError((e as Error).message);
      }
    }
    await reject(approval.id, reason.trim());
  }

  const workflowId = payloadString(approval.payload, "workflow_id");
  const paymentLimit = usageLimitOf(approval.payload);
  // WHICH provider account is about to be authorised — names and an e-mail
  // only (`provisioning_workflow.safe_account_details`), and only on a
  // re-connect, where the owner has more than one candidate in mind.
  const connectAccount = (approval.payload?.provider_account ?? null) as {
    display_name?: string | null;
    primary_email?: string | null;
  } | null;

  return (
    <li className="flex flex-col gap-2 px-3 py-2">
      <div className="flex items-center gap-2">
        <ShieldQuestion size={16} className="shrink-0" style={{ color: "var(--midnight)" }} />
        {showDetailLink ? (
          <a
            href={teamsDetailUrl("approval", approval.id)}
            onClick={(e) => handleShellLinkClick(e, teamsDetailUrl("approval", approval.id))}
            className="min-w-0 flex-1 truncate text-ui text-[var(--midnight)] hover:underline"
          >
            {title}
          </a>
        ) : (
          <span className="min-w-0 flex-1 truncate text-ui text-[var(--midnight)]">{title}</span>
        )}
        {approval.createdAt && (
          <span className="shrink-0 whitespace-nowrap text-caption text-[var(--midnight-muted)]">{timeAgo(approval.createdAt)}</span>
        )}
        {isActionable && !rejecting && (
          <div className="flex shrink-0 gap-2">
            {isCompanyStripeApproval(approval) && !stripeStatus ? (
              // Status not loaded yet: offer neither door set, or a load
              // would briefly read as "provisioning off" and show Approve.
              <EditorialButton size="sm" disabled>
                Loading…
              </EditorialButton>
            ) : isCompanyStripeApproval(approval) && stripeStatus?.provisioning_enabled ? (
              <>
                <EditorialButton
                  size="sm"
                  disabled={busy}
                  onClick={() =>
                    openMintedUrl(
                      async () =>
                        ((await api.post("/company/stripe/create", {})) as { onboarding_url: string })
                          .onboarding_url,
                      true,
                    )
                  }
                >
                  Set up Stripe
                </EditorialButton>
                <EditorialButton
                  size="sm"
                  disabled={busy}
                  onClick={() =>
                    openMintedUrl(
                      async () =>
                        ((await api.get("/company/stripe/connect/start")) as { authorize_url: string })
                          .authorize_url,
                      true,
                    )
                  }
                >
                  Connect existing
                </EditorialButton>
              </>
            ) : isProviderConsentApproval(approval) || isProviderInformationApproval(approval) ? (
              // Their own action button lives with the form below — a limit or
              // a set of answers has to travel with the click.
              null
            ) : isProviderConnectApproval(approval) ? (
              <>
                {connectAccount && (
                  <span className="text-ui text-[var(--midnight-muted)]">
                    {[connectAccount.display_name, connectAccount.primary_email]
                      .filter(Boolean)
                      .join(" · ")}
                  </span>
                )}
                <EditorialButton
                  size="sm"
                  disabled={busy}
                  onClick={() =>
                    openMintedUrl(
                      async () =>
                        (
                          (await api.post(
                            `/company/provisioning/workflows/${workflowId}/connect-link`,
                            {},
                          )) as { redirect_url?: string | null }
                        ).redirect_url,
                      false,
                    )
                  }
                >
                  {`Connect ${payloadString(approval.payload, "provider")}`}
                </EditorialButton>
                <EditorialButton size="sm" disabled={busy} onClick={() => run(() => approve(approval.id))}>
                  Approve
                </EditorialButton>
              </>
            ) : isPurchaseRequestApproval(approval) ? (
              <>
                <span className="text-ui text-[var(--midnight-muted)]">
                  {payloadString(approval.payload, "amount_display")} · {payloadString(approval.payload, "merchant")}
                </span>
                <EditorialButton
                  size="sm"
                  disabled={busy}
                  onClick={() =>
                    // The Link URL is minted for THIS click; the card never carries one.
                    openMintedUrl(
                      async () =>
                        (
                          (await api.post(
                            `/company/link-wallet/purchases/${payloadString(approval.payload, "request_id")}/approval-link`,
                            {},
                          )) as { approvalUrl?: string | null }
                        ).approvalUrl,
                      false,
                    )
                  }
                >
                  Approve in Link
                </EditorialButton>
              </>
            ) : isPaymentSetupApproval(approval) ? (
              <>
                <span className="text-ui text-[var(--midnight-muted)]">
                  {payloadString(approval.payload, "price_display")}
                  {paymentLimit ? ` · Limit ${formatUsageLimit(paymentLimit)}` : ""}
                </span>
                <EditorialButton
                  size="sm"
                  disabled={busy}
                  onClick={() =>
                    openMintedUrl(
                      async () =>
                        (
                          (await api.post(
                            `/company/provisioning/workflows/${workflowId}/payment-link`,
                            {},
                          )) as { checkout_session_url?: string | null }
                        ).checkout_session_url,
                      false,
                    )
                  }
                >
                  Add a card
                </EditorialButton>
                <EditorialButton size="sm" disabled={busy} onClick={() => run(() => approve(approval.id))}>
                  Approve
                </EditorialButton>
              </>
            ) : (
              <>
                {isCompanyStripeApproval(approval) && (
                  <EditorialButton size="sm" disabled={busy} onClick={() => navigateShell(teamsPanelUrl("accounts"))}>
                    Open Accounts
                  </EditorialButton>
                )}
                <EditorialButton size="sm" disabled={busy} onClick={() => run(() => approve(approval.id))}>
                  Approve
                </EditorialButton>
              </>
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
      {isActionable && !rejecting && isProviderConsentApproval(approval) && (
        <ProvisioningConsentActions approval={approval} busy={busy} run={run} approve={approve} />
      )}
      {isActionable && !rejecting && isProviderInformationApproval(approval) && (
        <ProvisioningInformationForm approval={approval} busy={busy} run={run} approve={approve} />
      )}
      {isActionable && !rejecting && <AskSomeoneToDecide approvalId={approval.id} decision={title} />}
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
                void run(rejectWithReason);
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
            onClick={() => run(rejectWithReason)}
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
