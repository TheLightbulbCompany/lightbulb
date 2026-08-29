"use client";

import * as React from "react";
import { ApiError, useApi } from "@/lib/api";
import { capture } from "@/lib/analytics";

const REASON_COPY: Record<string, string> = {
  no_subscription: "Redeem your code or subscribe to join — the free month is the competition month.",
  not_activated: "Finish onboarding first — Friday's first run has to complete.",
  phase_waitlist: "Entries open on drop day.",
  phase_ended: "The competition has ended.",
};

export function JoinForm({ onJoined }: { onJoined: () => void }) {
  const api = useApi();
  const [name, setName] = React.useState("");
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
      const reason = err instanceof ApiError && typeof err.body === "object" && err.body && "detail" in err.body ? String((err.body as { detail: unknown }).detail) : "";
      setError(REASON_COPY[reason] ?? "Couldn't join — try again.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <form onSubmit={submit} aria-label="Join the competition">
      <label className="lc-field" htmlFor="launch-name">
        <span>Display name</span>
        <input id="launch-name" required minLength={2} maxLength={40} value={name} onChange={(e) => setName(e.target.value)} />
      </label>
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
