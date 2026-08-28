"use client";

import * as React from "react";
import { ApiError, useApi } from "@/lib/api";
import { capture } from "@/lib/analytics";

const KEY_RE = /^rk_(live|test)_[A-Za-z0-9]{16,}$/;

const REASON_COPY: Record<string, string> = {
  bad_prefix: "That's not a restricted key. Create one under Developers → API keys → Create restricted key.",
  stripe_auth: "Stripe rejected that key. Copy it again — it's only shown once.",
  stripe_permission: "That key can't read your balance. Give it Balance: Read and Charges: Read, then paste it again.",
  stripe_error: "Stripe didn't answer. Try again in a minute.",
  mixed_currency: "Your account settles in more than one currency — we can't rank it. Email support.",
  no_subscription: "You need an active subscription to enter.",
  not_activated: "Finish onboarding first — Friday's first run has to complete.",
  phase_waitlist: "Entries open on drop day.",
  phase_ended: "The competition has ended.",
};

export function EnterForm({ onEntered }: { onEntered: () => void }) {
  const api = useApi();
  const [name, setName] = React.useState("");
  const [key, setKey] = React.useState("");
  const [isPublic, setIsPublic] = React.useState(true);
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    const trimmed = key.trim();
    if (!KEY_RE.test(trimmed)) {
      setError(REASON_COPY.bad_prefix);
      return;
    }
    setBusy(true);
    try {
      await api.post("/launch/enter", { display_name: name.trim(), stripe_key: trimmed, public: isPublic });
      capture("launch_entered", { public: isPublic });
      onEntered();
    } catch (err) {
      const reason = err instanceof ApiError && typeof err.body === "object" && err.body && "detail" in err.body ? String((err.body as { detail: unknown }).detail) : "stripe_error";
      setError(REASON_COPY[reason] ?? "Couldn't enter — try again.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <form onSubmit={submit} aria-label="Enter the competition">
      <label className="lc-field" htmlFor="launch-name">
        <span>Display name</span>
        <input id="launch-name" required minLength={2} maxLength={40} value={name} onChange={(e) => setName(e.target.value)} />
      </label>
      <label className="lc-field" htmlFor="launch-key">
        <span>Stripe restricted key</span>
        <input id="launch-key" data-mono required autoComplete="off" spellCheck={false} value={key} onChange={(e) => setKey(e.target.value)} placeholder="rk_live_…" />
      </label>
      <ol className="lc-steps">
        <li>Stripe Dashboard → Developers → API keys → <b>Create restricted key</b></li>
        <li>Permissions: <b>Balance: Read</b> and <b>Charges: Read</b>. Nothing else.</li>
        <li>Paste it here. We only ever read totals; you can delete the key in Stripe any time.</li>
      </ol>
      <label className="lc-check">
        <input type="checkbox" checked={isPublic} onChange={(e) => setIsPublic(e.target.checked)} />
        Show me on the public board
      </label>
      {error ? <p role="alert" className="lc-alert">{error}</p> : null}
      <div className="lc-actions">
        <button type="submit" disabled={busy} className="ld-btn">{busy ? "Entering…" : "Enter the competition"} <span className="ld-btn-arrow" aria-hidden="true">→</span></button>
      </div>
    </form>
  );
}
