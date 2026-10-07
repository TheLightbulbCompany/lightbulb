"use client";

import * as React from "react";
import { useApi } from "@/lib/api";
import { joinErrorCopy } from "./JoinForm";
import { capture } from "@/lib/analytics";

/**
 * Rename yourself on the sheet after joining.
 *
 * This exists because joining is no longer gated on onboarding. `mark_joined`
 * writes `display_name` exactly once, so the first name an owner typed used to
 * be their permanent identity on a public leaderboard. That was fine while the
 * only way to join was after Friday had researched and named the company; it
 * is not fine now that we ask during provisioning, minutes after signup. The
 * backend writes both the sheet row and the board entry — see
 * `launch_service.rename_display_name`.
 *
 * Collapsed to a button until used: for the owners who already have the right
 * name (everyone who joined the old way) this should be a footnote, not a form.
 */
/** Reuses the join surface's refusal decoder — same endpoint family, same two
 * error envelopes — and only overrides the generic fallback, since "Couldn't
 * join" would be nonsense on a rename. */
function renameErrorCopy(err: unknown): string {
  const copy = joinErrorCopy(err);
  return copy === "Couldn't join — try again." ? "Couldn't rename — try again." : copy;
}

export function RenameForm({ current, onRenamed }: { current: string | null; onRenamed: (name: string) => void }) {
  const api = useApi();
  const [editing, setEditing] = React.useState(false);
  const [name, setName] = React.useState(current ?? "");
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    const next = name.trim();
    if (next.length < 2 || busy) return;
    setBusy(true);
    setError(null);
    try {
      await api.patch("/launch/display-name", { display_name: next });
      capture("launch_renamed");
      onRenamed(next);
      setEditing(false);
    } catch (err) {
      // The route maps LaunchRefused to a 400 whose detail is the bare reason
      // string, so `ended` / `not_joined` / `not_eligible` all arrive looking
      // retryable. Telling an owner whose competition has ended to "try again"
      // loops them forever. `joinErrorCopy` already decodes both envelopes
      // (409 {detail:{reason}} and 400 plain-string detail) plus the 429.
      setError(renameErrorCopy(err));
    } finally {
      setBusy(false);
    }
  };

  if (!editing) {
    return (
      <div className="lc-row" style={{ alignItems: "center", marginTop: 16 }}>
        <p className="lc-fine" style={{ flex: 1, minWidth: 0 }}>
          Shown on the board as <strong>{current || "your business"}</strong>.
        </p>
        <button type="button" className="ld-btn ld-btn--ghost" onClick={() => setEditing(true)}>
          Change
        </button>
      </div>
    );
  }

  return (
    <form onSubmit={submit} aria-label="Change your board name">
      <label className="lc-field" htmlFor="launch-rename">
        <span>Business name</span>
        <input
          id="launch-rename"
          required
          minLength={2}
          maxLength={40}
          placeholder="Your company name"
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
      </label>
      {error ? (
        <p role="alert" className="lc-alert">
          {error}
        </p>
      ) : null}
      <div className="lc-actions">
        <button type="submit" disabled={busy || name.trim().length < 2} className="ld-btn">
          {busy ? "Saving…" : "Save"}
        </button>
        <button
          type="button"
          className="ld-btn ld-btn--ghost"
          onClick={() => {
            setName(current ?? "");
            setError(null);
            setEditing(false);
          }}
        >
          Cancel
        </button>
      </div>
    </form>
  );
}
