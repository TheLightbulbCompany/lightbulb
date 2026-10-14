"use server";

import { adminPost, type ActionResult } from "./_shared";

/**
 * Server Actions for the per-user billing surface (admin dashboard).
 *
 * Each action POSTs to a backend `/admin/users/{user_id}/billing/*` route
 * with a fresh `Idempotency-Key` (CEO D1). A billing action that changed
 * nothing comes back as a 409 whose detail says why.
 */

export async function cancelSubscription(userId: string): Promise<ActionResult> {
  return adminPost(`/admin/users/${encodeURIComponent(userId)}/billing/cancel-subscription`);
}

export async function pauseSubscription(userId: string): Promise<ActionResult> {
  return adminPost(`/admin/users/${encodeURIComponent(userId)}/billing/pause-subscription`);
}

export async function resumeSubscription(userId: string): Promise<ActionResult> {
  return adminPost(`/admin/users/${encodeURIComponent(userId)}/billing/resume-subscription`);
}

export async function markFoundingTeam(userId: string): Promise<ActionResult> {
  return adminPost(`/admin/users/${encodeURIComponent(userId)}/billing/founding-team`);
}

export async function issueCredit(
  userId: string,
  amountCents: number,
  reason: string,
  requestId: string,
): Promise<ActionResult> {
  // requestId is one admin submission: a retry of it is deduplicated by Stripe,
  // a second deliberate credit (new requestId) is a new credit.
  return adminPost(
    `/admin/users/${encodeURIComponent(userId)}/billing/issue-credit`,
    { amount_cents: amountCents, reason, request_id: requestId },
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

