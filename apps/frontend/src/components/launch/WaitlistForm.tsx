"use client";

import * as React from "react";
import { capture } from "@/lib/analytics";
import { fetchLaunchInfo, joinWaitlist, type LaunchInfo } from "@/lib/launch";
import { ReferralLink } from "./ReferralLink";
import { useLaunchRef } from "./useLaunchRef";
import "./launch.css";

const ERROR_COPY: Record<string, string> = {
  invalid_email: "Enter a valid email address.",
  waitlist_closed: "The waitlist has closed — sign up instead.",
  rate_limited: "Too many attempts. Try again in a minute.",
};

/**
 * Self-contained waitlist capture, styled as the landing `.ld-ask` pill so it
 * sits inside the brand hero unchanged. Drop it anywhere; it fetches its own
 * state and renders NOTHING unless the launch is configured and the phase is
 * `waitlist`, so it can be left in place through the drop.
 */
export function WaitlistForm() {
  return (
    <React.Suspense fallback={null}>
      <WaitlistFormInner />
    </React.Suspense>
  );
}

function WaitlistFormInner() {
  const refCode = useLaunchRef();
  const [info, setInfo] = React.useState<LaunchInfo | null | undefined>(undefined);
  const [email, setEmail] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const [joined, setJoined] = React.useState<{ position: number; referral_code: string; in_comp_window: boolean } | null>(null);

  React.useEffect(() => {
    let alive = true;
    fetchLaunchInfo().then((i) => alive && setInfo(i)).catch(() => alive && setInfo(null));
    return () => {
      alive = false;
    };
  }, []);

  if (!info || info.phase !== "waitlist") return null;

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const result = await joinWaitlist(email.trim(), refCode);
      setJoined(result);
      capture("launch_waitlist_joined", { position: result.position, referred: Boolean(refCode) });
    } catch (err) {
      const code = err instanceof Error ? err.message : "unknown";
      setError(ERROR_COPY[code] ?? "Something went wrong — try again.");
    } finally {
      setBusy(false);
    }
  };

  if (joined) {
    return (
      <div className="lc-card lc-card--lit" data-testid="waitlist-joined" style={{ maxWidth: 640, margin: "36px auto 0" }}>
        <p className="ld-funnel-h1" style={{ fontSize: 34 }}>You&apos;re #{joined.position}.</p>
        <p className="lc-fine">
          {joined.in_comp_window
            ? `You're in the first ${info.comp_slots} — your free competition month is yours if you finish onboarding within 3 days of the drop. Watch your inbox on drop day.`
            : `Those ${info.comp_slots} free-month spots are already claimed, but you can still compete — and every friend who subscribes earns you a month free.`}
        </p>
        <ReferralLink code={joined.referral_code} />
      </div>
    );
  }

  return (
    <div>
      <form className="ld-ask" onSubmit={submit} aria-label="Join the waitlist">
        <label className="sr-only" htmlFor="launch-email">Email</label>
        <input
          id="launch-email"
          type="email"
          required
          autoComplete="email"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          placeholder="you@company.com"
        />
        <button type="submit" className="ld-btn" disabled={busy || email.trim().length < 3}>
          {busy ? "Joining…" : "Join the waitlist"} <span className="ld-btn-arrow" aria-hidden="true">→</span>
        </button>
      </form>
      {error ? <p role="alert" className="lc-alert" style={{ textAlign: "center" }}>{error}</p> : null}
    </div>
  );
}
