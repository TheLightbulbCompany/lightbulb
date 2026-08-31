"use client";

import * as React from "react";
import { ApiError, useApi } from "@/lib/api";
import { capture } from "@/lib/analytics";
import type { LaunchEntry } from "@/lib/launch";

const KEY_RE = /^rk_(live|test)_[A-Za-z0-9]{16,}$/;

const REASON_COPY: Record<string, string> = {
  bad_prefix: "That's not a restricted key. Create one under Developers → API keys → Create restricted key.",
  stripe_auth: "Stripe rejected that key. Copy it again — it's only shown once.",
  stripe_permission: "That key can't read your balance. Give it Balance: Read and Charges: Read, then paste it again.",
  stripe_error: "Stripe didn't answer. Try again in a minute.",
  mixed_currency: "Your account settles in more than one currency — we can't rank it. Email support.",
  not_joined: "Join the competition first, then connect Stripe.",
  no_entry: "Join the competition first, then connect Stripe.",
};

export function ConnectStripeForm({ onConnected }: { onConnected: (entry: LaunchEntry) => void }) {
  const api = useApi();
  const [key, setKey] = React.useState("");
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
      const entry = (await api.post("/launch/connect-stripe", { stripe_key: trimmed })) as LaunchEntry;
      capture("launch_stripe_connected", {});
      onConnected(entry);
    } catch (err) {
      const reason = err instanceof ApiError && typeof err.body === "object" && err.body && "detail" in err.body ? String((err.body as { detail: unknown }).detail) : "stripe_error";
      setError(REASON_COPY[reason] ?? "Couldn't connect — try again.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <form onSubmit={submit} aria-label="Connect Stripe">
      <label className="lc-field" htmlFor="launch-key">
        <span>Stripe restricted key</span>
        <input id="launch-key" data-mono required autoComplete="off" spellCheck={false} value={key} onChange={(e) => setKey(e.target.value)} placeholder="rk_live_…" />
      </label>
      <ol className="lc-steps">
        <li>Stripe Dashboard → Developers → API keys → <b>Create restricted key</b></li>
        <li>Permissions: <b>Balance: Read</b> and <b>Charges: Read</b>. Nothing else.</li>
        <li>Paste it here. We only ever read totals; you can delete the key in Stripe any time.</li>
      </ol>
      {error ? <p role="alert" className="lc-alert">{error}</p> : null}
      <div className="lc-actions">
        <button type="submit" disabled={busy} className="ld-btn">{busy ? "Connecting…" : "Connect Stripe"} <span className="ld-btn-arrow" aria-hidden="true">→</span></button>
      </div>
    </form>
  );
}
