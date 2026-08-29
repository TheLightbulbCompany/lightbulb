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
  connect_stripe: "Connect Stripe",
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
  const base = TYPE_LABEL[type] ?? type;
  const subject = approvalSubject(payload);
  return subject ? `${base}: ${subject}` : base;
}
