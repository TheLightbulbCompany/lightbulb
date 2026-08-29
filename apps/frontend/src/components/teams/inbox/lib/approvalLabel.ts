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
  approve_ceo_strategy: "CEO Strategy",
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

/** P0b's two provisioning cards, filed under the same enum for the same
 *  reason: the payload marker is what identifies them as ours. */
const STRIPE_CLI_PAIR_ACTION = "stripe_cli_login";
const PROVIDER_CONSENT_ACTION = "provider_consent";

export function isStripeCliPairApproval(
  approval?: { payload?: Record<string, unknown> | null } | null,
): boolean {
  return approval?.payload?.isol8_action === STRIPE_CLI_PAIR_ACTION;
}

export function isProviderConsentApproval(
  approval?: { payload?: Record<string, unknown> | null } | null,
): boolean {
  return approval?.payload?.isol8_action === PROVIDER_CONSENT_ACTION;
}

/** Row label per marker — beats a growing ternary chain over the predicates. */
const ACTION_LABEL: Record<string, string> = {
  [COMPANY_STRIPE_ACTION]: "Connect Stripe",
  [STRIPE_CLI_PAIR_ACTION]: "Pair Stripe CLI",
  [PROVIDER_CONSENT_ACTION]: "Approve provider",
};

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
