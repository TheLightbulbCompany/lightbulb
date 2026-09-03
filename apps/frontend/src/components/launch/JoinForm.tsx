"use client";

import * as React from "react";
import { ApiError, useApi } from "@/lib/api";
import { capture } from "@/lib/analytics";

const REASON_COPY: Record<string, string> = {
  // DEPLOY-SKEW ONLY. The backend no longer refuses an unonboarded join — the
  // sign-up modal asks during provisioning on purpose. But Vercel ships this
  // file independently of (and usually ahead of) the ECS backend, so for the
  // length of a deploy window the OLD backend still answers 409 not_onboarded.
  // Without this entry those users get "Couldn't join — try again." on a
  // refusal that is actually explainable. Safe to delete once the backend is
  // deployed everywhere.
  not_onboarded: "Finish onboarding first — Friday's first run has to complete.",
  closed: "The sheet has closed.",
  org_not_supported: "Switch to your personal workspace to join.",
  no_email: "We couldn't find an email on your account — contact support.",
  // Board-action refusals (`_sheet_row_for`), shared with RenameForm. None of
  // these are retryable, so they must never fall through to the generic
  // "try again" — that loops the owner on a decision that is already final.
  ended: "The competition has ended — the board is final.",
  not_joined: "You're not on the sheet yet.",
  not_eligible: "You're not in the competition, so the board is read-only for you.",
};

/** POST /launch/join refusals arrive two ways: 409s carry
 * `{detail: {reason: "…"}}`, 400s a plain-string detail, 429 a rate limit.
 * Shared with the in-chat join banner. */
export function joinErrorCopy(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status === 429) return "Too many tries — wait a minute and try again.";
    if (err.body && typeof err.body === "object" && "detail" in err.body) {
      const detail = (err.body as { detail: unknown }).detail;
      const reason =
        typeof detail === "string"
          ? detail
          : detail && typeof detail === "object" && "reason" in detail
            ? String((detail as { reason: unknown }).reason)
            : "";
      if (REASON_COPY[reason]) return REASON_COPY[reason];
    }
  }
  return "Couldn't join — try again.";
}

export function JoinForm({ onJoined, suggestedName }: { onJoined: () => void; suggestedName?: string | null }) {
  const api = useApi();
  // `suggestedName` (SheetCard's `me.suggested_display_name`, the owner's
  // live Paperclip company name) is only ever passed once `me` has already
  // loaded — SheetCard doesn't render this form until then — so a plain
  // initializer is enough; it never needs to react to a later prop change.
  const [name, setName] = React.useState(suggestedName ?? "");
  const [isPublic, setIsPublic] = React.useState(true);
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      await api.post("/launch/join", { display_name: name.trim(), public: isPublic });
      capture("launch_joined", { public: isPublic });
      onJoined();
    } catch (err) {
      setError(joinErrorCopy(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <form onSubmit={submit} aria-label="Join the competition">
      <label className="lc-field" htmlFor="launch-name">
        <span>Display name</span>
        <input id="launch-name" required minLength={2} maxLength={40} placeholder="Your company name" value={name} onChange={(e) => setName(e.target.value)} />
      </label>
      <p className="lc-fine" style={{ marginTop: 6 }}>Shown on the public board</p>
      <label className="lc-check">
        <input type="checkbox" checked={isPublic} onChange={(e) => setIsPublic(e.target.checked)} />
        Show me on the public board
      </label>
      {error ? <p role="alert" className="lc-alert">{error}</p> : null}
      <div className="lc-actions">
        <button type="submit" disabled={busy} className="ld-btn">{busy ? "Joining…" : "Join the competition"} <span className="ld-btn-arrow" aria-hidden="true">→</span></button>
      </div>
    </form>
  );
}
