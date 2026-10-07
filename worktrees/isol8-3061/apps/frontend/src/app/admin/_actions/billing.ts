"use server";

import { adminPost, type ActionResult } from "./_shared";

/**
 * Server Actions for the per-user billing surface (admin dashboard).
 *
 * Each action POSTs to a backend `/admin/users/{user_id}/billing/*` route
 * with a fresh `Idempotency-Key` (CEO D1) — duplicate clicks return the
 * cached response instead of double-charging or double-cancelling.
 */

export async function cancelSubscription(userId: string): Promise<ActionResult> {
  return adminPost(`/admin/users/${encodeURIComponent(userId)}/billing/cancel-subscription`);
}

export async function pauseSubscription(userId: string): Promise<ActionResult> {
  return adminPost(`/admin/users/${encodeURIComponent(userId)}/billing/pause-subscription`);
}

export async function issueCredit(
  userId: string,
  amountCents: number,
  reason: string,
): Promise<ActionResult> {
  return adminPost(
    `/admin/users/${encodeURIComponent(userId)}/billing/issue-credit`,
    { amount_cents: amountCents, reason },
  );
}

export async function markInvoiceResolved(
  userId: string,
  invoiceId: string,
): Promise<ActionResult> {
  return adminPost(
    `/admin/users/${encodeURIComponent(userId)}/billing/mark-invoice-resolved`,
    { invoice_id: invoiceId },
  );
}

