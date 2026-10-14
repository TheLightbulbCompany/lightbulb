"use client";

import { useState } from "react";

import { EditorialButton } from "@/components/teams/shell";
import { useApi } from "@/lib/api";
import { notifyError } from "@/lib/notify";
import { payloadString } from "@/components/teams/inbox/lib/approvalLabel";
import type { Approval } from "@/components/teams/shared/types";

/**
 * The consent card: the guide's "Collect informed approval" set, rendered.
 *
 * Approving here is TWO writes in order — our workflow route first (it records
 * who signed and the limit they signed for), Paperclip's approve second. If
 * ours fails the card stays pending, because a Paperclip approval the backend
 * never saw would strand the workflow in `awaiting_approval` forever.
 */

export const MISSING_WORKFLOW_MSG =
  "This card has lost the request it belongs to — refresh your inbox and approve it again.";

export type UsageLimit = { max_amount: number; currency: string; recurring_interval: string };

/** The limit as it sits on the card, or null when the card carries none. */
export function usageLimitOf(payload: Record<string, unknown> | null | undefined): UsageLimit | null {
  const raw = payload?.usage_limit;
  if (!raw || typeof raw !== "object") return null;
  const limit = raw as Partial<UsageLimit>;
  if (typeof limit.max_amount !== "number" || !Number.isFinite(limit.max_amount)) return null;
  return {
    max_amount: limit.max_amount,
    currency: typeof limit.currency === "string" ? limit.currency : "usd",
    recurring_interval:
      typeof limit.recurring_interval === "string" ? limit.recurring_interval : "month",
  };
}

/** Cents → "$50.00 / month". The rail is USD-only (`UsageLimit` in the router). */
export function formatUsageLimit(limit: UsageLimit): string {
  return `$${(limit.max_amount / 100).toFixed(2)} / ${limit.recurring_interval}`;
}

/** An agent-authored link is a click target, so it renders only as https://. */
function httpsUrl(payload: Record<string, unknown> | null | undefined, key: string): string | null {
  const value = payloadString(payload, key);
  return value.startsWith("https://") ? value : null;
}

/** The configuration the agent asked for, which the owner is approving along
 *  with the spend. Scalars only — the router already refuses anything else
 *  (`_validate`), so a nested value here is not a field we filed. */
function configurationLines(payload: Record<string, unknown> | null | undefined): string[] {
  const raw = payload?.configuration;
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return [];
  return Object.entries(raw as Record<string, unknown>)
    .filter(([, v]) => typeof v === "string" || typeof v === "number" || typeof v === "boolean")
    .map(([k, v]) => `${k}: ${String(v)}`);
}

function factLine(payload: Record<string, unknown> | null | undefined): string {
  const categories = Array.isArray(payload?.categories)
    ? (payload!.categories as unknown[]).filter((c) => typeof c === "string").join(", ")
    : "";
  const facts = [
    categories,
    payloadString(payload, "availability") && `Available: ${payloadString(payload, "availability")}`,
    payloadString(payload, "scope") && `Scope: ${payloadString(payload, "scope")}`,
    payloadString(payload, "environment"),
    payloadString(payload, "payment_owner") && `Paid with: ${payloadString(payload, "payment_owner")}`,
    payloadString(payload, "action") && `This ${payloadString(payload, "action")}`,
  ].filter(Boolean);
  return facts.join(" · ");
}

export function ProvisioningConsentActions({
  approval,
  busy,
  run,
  approve,
}: {
  approval: Approval;
  busy: boolean;
  run: (fn: () => Promise<void>) => void;
  approve: (approvalId: string) => Promise<void>;
}) {
  const api = useApi();
  const payload = approval.payload;
  const limit = usageLimitOf(payload);
  const editable = payload?.usage_limit_editable === true && limit !== null;
  const [dollars, setDollars] = useState(limit ? (limit.max_amount / 100).toFixed(2) : "");

  const workflowId = payloadString(payload, "workflow_id");
  const price = payloadString(payload, "price_display");
  const description = payloadString(payload, "description");
  const reason = payloadString(payload, "reason");
  const note = payloadString(payload, "usage_limit_note");
  const constraints = Array.isArray(payload?.constraints)
    ? (payload!.constraints as unknown[]).filter((c) => typeof c === "string")
    : [];
  const facts = factLine(payload);
  const configuration = configurationLines(payload);
  const terms = httpsUrl(payload, "terms_url");
  const privacy = httpsUrl(payload, "privacy_policy_url");
  const website = httpsUrl(payload, "website_url");
  const inputId = `consent-limit-${approval.id}`;

  /** The limit the owner is actually signing for: the typed dollars when the
   *  card says it is still editable, the card's own number otherwise. */
  function signedLimit(): UsageLimit | null {
    if (!limit) return null;
    if (!editable) return limit;
    const cents = Math.round(Number(dollars) * 100);
    return Number.isFinite(cents) && cents > 0 ? { ...limit, max_amount: cents } : limit;
  }

  return (
    <div className="flex flex-col gap-1 pl-6 text-ui">
      <span className="text-[var(--midnight)]">
        {payloadString(payload, "provider")} · {payloadString(payload, "service")}
        {price ? ` · ${price}` : ""}
      </span>
      {description && <p className="text-[var(--midnight-muted)]">{description}</p>}
      {reason && <p className="text-[var(--midnight-muted)]">Your CEO asked because: {reason}</p>}
      {facts && <p className="text-caption text-[var(--midnight-muted)]">{facts}</p>}
      {configuration.length > 0 && (
        <p className="text-caption text-[var(--midnight-muted)]">
          Configuration: {configuration.join(", ")}
        </p>
      )}
      {constraints.length > 0 && (
        <p className="text-caption text-[var(--midnight-muted)]">Constraints: {constraints.join(", ")}</p>
      )}
      {(terms || privacy || website) && (
        <p className="flex gap-3 text-ui">
          {terms && (
            <a className="underline" href={terms} target="_blank" rel="noopener noreferrer">
              Terms
            </a>
          )}
          {privacy && (
            <a className="underline" href={privacy} target="_blank" rel="noopener noreferrer">
              Privacy
            </a>
          )}
          {website && (
            <a className="underline" href={website} target="_blank" rel="noopener noreferrer">
              Website
            </a>
          )}
        </p>
      )}
      {limit &&
        (editable ? (
          <span className="flex items-center gap-2 text-ui">
            <label htmlFor={inputId} className="text-[var(--midnight-muted)]">
              Spending limit ($ / {limit.recurring_interval})
            </label>
            <input
              id={inputId}
              type="number"
              min="0"
              step="0.01"
              value={dollars}
              disabled={busy}
              onChange={(e) => setDollars(e.target.value)}
              className="w-24 bg-transparent outline-none"
              style={{ borderBottom: "1px solid var(--edge)", color: "var(--midnight)" }}
            />
          </span>
        ) : (
          <p className="text-ui text-[var(--midnight-muted)]">
            Spending limit: {formatUsageLimit(limit)}
            {note ? ` — ${note}` : ""}
          </p>
        ))}
      <span>
        <EditorialButton
          size="sm"
          disabled={busy}
          onClick={() =>
            run(async () => {
              // Ours first: a Paperclip approval the backend never recorded
              // leaves the workflow waiting for an approval it already has.
              // With no id there is no first write, and approving anyway would
              // silently degrade a two-write decision to one.
              if (!workflowId) {
                notifyError(MISSING_WORKFLOW_MSG);
                return;
              }
              const signed = signedLimit();
              await api.post(
                `/company/provisioning/workflows/${workflowId}/approve`,
                editable && signed ? { usage_limit: signed } : {},
              );
              await approve(approval.id);
            })
          }
        >
          Approve
        </EditorialButton>
      </span>
    </div>
  );
}
