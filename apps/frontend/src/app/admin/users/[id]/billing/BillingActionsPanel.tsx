"use client";

import * as React from "react";
import { useRouter } from "next/navigation";

import { Card } from "@/components/admin/Card";
import { ConfirmActionDialog } from "@/components/admin/ConfirmActionDialog";
import { ErrorBanner } from "@/components/admin/ErrorBanner";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  cancelSubscription,
  issueCredit,
  markInvoiceResolved,
  pauseSubscription,
} from "@/app/admin/_actions/billing";

export interface BillingActionsPanelProps {
  userId: string;
  /** Primary email for the user — required because the destructive confirms type-check against it. */
  email: string | null;
}

/**
 * Client island for the per-user billing actions. Each destructive action is
 * gated by the typed-confirmation dialog (CEO S5). The credit + invoice
 * actions need a small inline form; we render the input as a sibling of the
 * trigger and snapshot its value when the user opens the dialog.
 */
export function BillingActionsPanel({ userId, email }: BillingActionsPanelProps) {
  const router = useRouter();
  const [error, setError] = React.useState<string | null>(null);
  const [notice, setNotice] = React.useState<string | null>(null);

  const [creditAmount, setCreditAmount] = React.useState("");
  const [creditReason, setCreditReason] = React.useState("");
  const [invoiceId, setInvoiceId] = React.useState("");
  // One submission id per credit (amount + reason) until a definitive outcome.
  // A lost response may mean Stripe already created the credit, so a retry of
  // the same credit must reuse the id (Stripe returns the earlier one).
  const creditSubmission = React.useRef<{ key: string; id: string } | null>(null);

  // Without an email we can't enforce the typed-confirmation gate the way the
  // spec requires. Surface the constraint instead of silently degrading.
  const noEmailWarning =
    email && email.length > 0 ? null : (
      <ErrorBanner
        error="No primary email on file — destructive subscription actions are disabled until Clerk reports one."
        variant="warning"
      />
    );

  function clearStatus() {
    setError(null);
    setNotice(null);
  }

  function applyResult(label: string, ok: boolean, errMsg?: string) {
    if (ok) {
      setNotice(`${label} succeeded.`);
      router.refresh();
    } else {
      setError(`${label} failed: ${errMsg ?? "unknown_error"}`);
    }
  }

  // Handlers are plain async functions — React Compiler handles memoization
  // automatically and keeps the local closures readable (no manual deps array
  // to maintain when state references change).
  async function handleCancel() {
    clearStatus();
    const result = await cancelSubscription(userId);
    applyResult("Cancel subscription", result.ok, result.error);
  }

  async function handlePause() {
    clearStatus();
    const result = await pauseSubscription(userId);
    applyResult("Pause subscription", result.ok, result.error);
  }

  async function handleCredit() {
    clearStatus();
    const cents = Math.round(Number(creditAmount));
    if (!Number.isFinite(cents) || cents <= 0) {
      setError("Issue credit failed: amount must be a positive integer (cents).");
      return;
    }
    if (creditReason.trim().length === 0) {
      setError("Issue credit failed: reason is required.");
      return;
    }
    const key = `${cents}:${creditReason.trim()}`;
    if (creditSubmission.current?.key !== key) {
      creditSubmission.current = { key, id: crypto.randomUUID() };
    }
    const result = await issueCredit(userId, cents, creditReason.trim(), creditSubmission.current.id);
    // Definitive: a success, or a 4xx the server refused before any write.
    // Network errors and 5xx keep the id so a retry cannot credit twice.
    if (result.ok || (result.status >= 400 && result.status < 500)) {
      creditSubmission.current = null;
    }
    if (result.ok && (result.data as { status?: string } | undefined)?.status === "duplicate") {
      setNotice("This credit was already issued — Stripe returned the earlier one; no new credit was created.");
    } else if (result.ok) {
      setNotice("Credit issued.");
      setCreditAmount("");
      setCreditReason("");
      router.refresh();
    } else {
      setError(`Issue credit failed: ${result.error ?? "unknown_error"}`);
    }
  }

  async function handleInvoice() {
    clearStatus();
    const id = invoiceId.trim();
    if (id.length === 0) {
      setError("Mark invoice resolved failed: invoice_id is required.");
      return;
    }
    const result = await markInvoiceResolved(userId, id);
    if (result.ok) {
      setNotice(`Invoice ${id} marked resolved.`);
      setInvoiceId("");
      router.refresh();
    } else {
      setError(`Mark invoice resolved failed: ${result.error ?? "unknown_error"}`);
    }
  }

  const subscriptionConfirm = email ?? `${userId}-no-email`;
  const subscriptionDisabled = !email;

  return (
    <Card title="Billing actions">
      <div className="space-y-4">
        {noEmailWarning}
        {error ? <ErrorBanner error={error} variant="error" /> : null}
        {notice ? <ErrorBanner error={notice} variant="info" /> : null}

        {/* Subscription lifecycle */}
        <div className="flex flex-wrap gap-2">
          <ConfirmActionDialog
            confirmText={subscriptionConfirm}
            actionLabel="Cancel subscription"
            destructive
            onConfirm={handleCancel}
          >
            <Button type="button" variant="destructive" size="sm" disabled={subscriptionDisabled}>
              Cancel subscription
            </Button>
          </ConfirmActionDialog>
          <ConfirmActionDialog
            confirmText={subscriptionConfirm}
            actionLabel="Pause subscription"
            onConfirm={handlePause}
          >
            <Button type="button" variant="outline" size="sm" disabled={subscriptionDisabled}>
              Pause subscription
            </Button>
          </ConfirmActionDialog>
        </div>

        {/* Issue credit */}
        <div className="space-y-2 rounded-lg border border-border p-3">
          <h3 className="text-xs font-medium uppercase tracking-caps text-muted-foreground">
            Issue credit
          </h3>
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-[160px_1fr_auto]">
            <Input
              type="number"
              min={1}
              step={1}
              placeholder="amount (cents)"
              value={creditAmount}
              onChange={(e) => setCreditAmount(e.target.value)}
              aria-label="Credit amount in cents"
            />
            <Input
              type="text"
              placeholder="reason (audited)"
              value={creditReason}
              onChange={(e) => setCreditReason(e.target.value)}
              aria-label="Credit reason"
            />
            <ConfirmActionDialog
              confirmText={userId}
              actionLabel="Issue credit"
              onConfirm={handleCredit}
            >
              <Button
                type="button"
                variant="default"
                size="sm"
                disabled={creditAmount.length === 0 || creditReason.trim().length === 0}
              >
                Issue credit
              </Button>
            </ConfirmActionDialog>
          </div>
        </div>

        {/* Mark invoice resolved */}
        <div className="space-y-2 rounded-lg border border-border p-3">
          <h3 className="text-xs font-medium uppercase tracking-caps text-muted-foreground">
            Mark invoice resolved
          </h3>
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-[1fr_auto]">
            <Input
              type="text"
              placeholder="in_..."
              value={invoiceId}
              onChange={(e) => setInvoiceId(e.target.value)}
              aria-label="Stripe invoice id"
            />
            <ConfirmActionDialog
              confirmText={userId}
              actionLabel="Mark invoice resolved"
              onConfirm={handleInvoice}
            >
              <Button
                type="button"
                variant="default"
                size="sm"
                disabled={invoiceId.trim().length === 0}
              >
                Mark resolved
              </Button>
            </ConfirmActionDialog>
          </div>
        </div>
      </div>
    </Card>
  );
}
