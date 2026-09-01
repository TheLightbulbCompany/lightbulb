"use client";

import * as React from "react";
import { ApiError, useApi } from "@/lib/api";
import { capture } from "@/lib/analytics";
import type { LaunchTeamInterest } from "@/lib/launch";

/**
 * "Entering as a team?" — deliberately the subordinate path on /launch.
 *
 * Teams do NOT self-serve: POST /launch/join still refuses an org context,
 * and this form does not join anything. It emails the founders, who enter the
 * team by hand, so every string here has to say so — an owner who reads this
 * as "I'm in" is a support ticket on September 30.
 *
 * A native <details> is the disclosure (no state, no library); signed-out
 * visitors see the same affordance and are asked to sign in, because the POST
 * is authed — that is what identifies the submitter and keeps the form off
 * the spam list.
 */
export function TeamInterestForm({ signedIn }: { signedIn: boolean }) {
  const api = useApi();
  const [teamName, setTeamName] = React.useState("");
  const [seats, setSeats] = React.useState("");
  const [note, setNote] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const [sent, setSent] = React.useState<LaunchTeamInterest | null>(null);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      const saved = (await api.post("/launch/team-interest", {
        team_name: teamName.trim(),
        seats: Number(seats),
        note: note.trim(),
      })) as LaunchTeamInterest;
      capture("launch_team_interest", { seats: Number(seats) });
      setSent(saved);
    } catch (err) {
      if (err instanceof ApiError && err.status === 429) setError("Too many tries — wait a minute and try again.");
      else if (err instanceof ApiError && err.status === 422) setError("Check the team name and seat count.");
      else setError("Couldn't send that — try again.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <details className="lc-disclosure" data-testid="team-interest">
      <summary>Entering as a team?</summary>
      {sent ? (
        <p className="lc-fine" role="status">
          Got it — {sent.team_name}, {sent.seats} seat{sent.seats === 1 ? "" : "s"}. A founder will email you to sort
          out the entry. Nothing is entered yet — they will confirm the details with you.
        </p>
      ) : !signedIn ? (
        <p className="lc-fine">
          Teams are entered by hand: tell us about yours and a founder gets in touch. Sign in first (the buttons
          above), so we know who to reply to.
        </p>
      ) : (
        <form onSubmit={submit} aria-label="Tell us about your team">
          <p className="lc-fine">
            Teams don&apos;t join the sheet themselves. Tell us about yours and a founder will get in touch — this
            sends your details, it doesn&apos;t enter you.
          </p>
          <label className="lc-field" htmlFor="team-name">
            <span>Team or company</span>
            <input
              id="team-name"
              required
              minLength={2}
              maxLength={80}
              value={teamName}
              onChange={(e) => setTeamName(e.target.value)}
            />
          </label>
          <label className="lc-field" htmlFor="team-seats">
            <span>Seats</span>
            <input
              id="team-seats"
              type="number"
              required
              min={1}
              max={500}
              step={1}
              value={seats}
              onChange={(e) => setSeats(e.target.value)}
            />
          </label>
          <label className="lc-field" htmlFor="team-note">
            <span>Anything we should know (optional)</span>
            <textarea id="team-note" rows={3} maxLength={1000} value={note} onChange={(e) => setNote(e.target.value)} />
          </label>
          {error ? (
            <p role="alert" className="lc-alert">
              {error}
            </p>
          ) : null}
          <div className="lc-actions">
            <button type="submit" disabled={busy} className="ld-btn">
              {busy ? "Sending…" : "Tell the founders"}{" "}
              <span className="ld-btn-arrow" aria-hidden="true">
                →
              </span>
            </button>
          </div>
        </form>
      )}
    </details>
  );
}
