// apps/frontend/src/components/teams/inbox/lib/approvalLabel.ts

// Ported from upstream Paperclip's approvalLabel
// (paperclip/ui/src/components/ApprovalPayload.tsx) (MIT, © 2025 Paperclip AI).
//
// The approval list row carries no top-level `title` — the human-readable label
// is synthesized from the approval `type` (mapped to a friendly name) plus a
// subject pulled from the redacted `payload`. Reading `approval.title` directly
// always fell back to a generic "Approval requested"; this restores the
// contextual label, e.g. "Hire Agent: Designer".

/** Friendly names for the known upstream approval types. */
const TYPE_LABEL: Record<string, string> = {
  hire_agent: "Hire Agent",
  approve_ceo_strategy: "Strategy",
  budget_override_required: "Budget Override",
  request_board_approval: "Board Approval",
};

/** Our company-Stripe card's payload marker. Paperclip validates `type`
 *  against its own four-value enum, so the card is filed as
 *  `request_board_approval` and this marker is what identifies it as ours. */
const COMPANY_STRIPE_ACTION = "company_stripe";

export function isCompanyStripeApproval(
  approval?: { payload?: Record<string, unknown> | null } | null,
): boolean {
  return approval?.payload?.isol8_action === COMPANY_STRIPE_ACTION;
}

/** P0b's provisioning consent card, filed under the same enum for the same
 *  reason: the payload marker is what identifies it as ours. */
const PROVIDER_CONSENT_ACTION = "provider_consent";

export function isProviderConsentApproval(
  approval?: { payload?: Record<string, unknown> | null } | null,
): boolean {
  return approval?.payload?.isol8_action === PROVIDER_CONSENT_ACTION;
}

/** The rest of P0b's rail. Each is a different human decision — connect the
 *  provider account, answer the provider's form, put a card on file — so each
 *  is its own card with its own marker. */
const PROVIDER_CONNECT_ACTION = "provider_connect";
const PROVIDER_INFORMATION_ACTION = "provider_information";
const PAYMENT_SETUP_ACTION = "payment_setup";

export function isProviderConnectApproval(
  approval?: { payload?: Record<string, unknown> | null } | null,
): boolean {
  return approval?.payload?.isol8_action === PROVIDER_CONNECT_ACTION;
}

export function isProviderInformationApproval(
  approval?: { payload?: Record<string, unknown> | null } | null,
): boolean {
  return approval?.payload?.isol8_action === PROVIDER_INFORMATION_ACTION;
}

export function isPaymentSetupApproval(
  approval?: { payload?: Record<string, unknown> | null } | null,
): boolean {
  return approval?.payload?.isol8_action === PAYMENT_SETUP_ACTION;
}

/** A purchase an agent asked for (Link Agent Wallet). */
const PURCHASE_REQUEST_ACTION = "purchase_request";

export function isPurchaseRequestApproval(
  approval?: { payload?: Record<string, unknown> | null } | null,
): boolean {
  return approval?.payload?.isol8_action === PURCHASE_REQUEST_ACTION;
}

/** Row label per marker — beats a growing ternary chain over the predicates. */
const ACTION_LABEL: Record<string, string> = {
  [COMPANY_STRIPE_ACTION]: "Connect Stripe",
  [PROVIDER_CONSENT_ACTION]: "Approve provider",
  [PROVIDER_CONNECT_ACTION]: "Connect provider",
  [PROVIDER_INFORMATION_ACTION]: "Provider needs info",
  [PAYMENT_SETUP_ACTION]: "Add a card",
  [PURCHASE_REQUEST_ACTION]: "Purchase",
};

/** One string field off an untrusted, agent-authored payload. */
export function payloadString(
  payload: Record<string, unknown> | null | undefined,
  key: string,
): string {
  const value = payload?.[key];
  return typeof value === "string" ? value : "";
}

function firstNonEmptyString(...values: unknown[]): string | null {
  for (const value of values) {
    if (typeof value === "string" && value.trim().length > 0) {
      return value.trim();
    }
  }
  return null;
}

/** The subject line drawn from a payload (title / name / summary / action). */
export function approvalSubject(
  payload?: Record<string, unknown> | null,
): string | null {
  return firstNonEmptyString(
    payload?.title,
    payload?.name,
    payload?.summary,
    payload?.recommendedAction,
  );
}

/**
 * Build a contextual label for an approval, e.g. "Hire Agent: Designer".
 * Falls back to the friendly type name, then the raw type, then a generic
 * "Approval requested" when no type is present.
 */
export function approvalLabel(
  type?: string | null,
  payload?: Record<string, unknown> | null,
): string {
  if (!type) return "Approval requested";
  // Both keys come off an untrusted payload, so read them as OWN properties —
  // a plain `map[key]` resolves "constructor"/"toString" to something that is
  // not a label.
  const action = payload?.isol8_action;
  const byAction =
    typeof action === "string" && Object.hasOwn(ACTION_LABEL, action) ? ACTION_LABEL[action] : undefined;
  const byType = Object.hasOwn(TYPE_LABEL, type) ? TYPE_LABEL[type] : undefined;
  const base = byAction ?? byType ?? type;
  const subject = approvalSubject(payload);
  return subject ? `${base}: ${subject}` : base;
}
