/**
 * First-run eligibility host — the mobile counterpart of the web's
 * ProvisioningStepper branch (`isTrialLocal && gatePhase === "normal" &&
 * !firstRun.briefSubmitted → <FirstRunFunnel/>`).
 *
 * Mounts INSIDE ProvisionGate, so blocked/team-pending interstitials always
 * win (the web's gatePhase === "normal" requirement). Renders the funnel
 * full-screen INSTEAD of the tab shell for an eligible user; everyone else
 * falls through to children immediately.
 *
 * Eligibility = personal `trial_local` owner who hasn't submitted the
 * brief. The briefSubmitted check is a local Clerk-metadata read, so every
 * onboarded user skips the billing probe entirely. For a brand-new signup
 * the `trial_local` row is created as a side effect of the very first
 * `GET /container/status` (which ProvisionGate is polling in parallel), so
 * a 404 from /billing/account here usually means "not yet" — re-probe on a
 * provisioning-window cadence, bounded so a genuinely row-less account
 * (org admin mid-checkout) eventually falls through to the normal shell.
 * 403 (org member) and other errors are definitive: not eligible.
 */
import { useAuth, useUser } from "@clerk/clerk-expo";
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { ApiError, createApiClient } from "@isol8/shared";

import { makeAdapter } from "../../lib/adapter";
import { readFirstRun } from "../../lib/firstRun";
import { FirstRunFunnel } from "./FirstRunFunnel";

const PROBE_MS = 5_000;
const MAX_PROBES = 24; // ~2 minutes — covers the provisioning window.

export function FirstRunGate({ children }: { children: ReactNode }) {
  const { user } = useUser();
  const { getToken } = useAuth();
  const getTokenRef = useRef(getToken);
  getTokenRef.current = getToken;
  const api = useMemo(
    () => createApiClient(makeAdapter(() => getTokenRef.current())),
    [],
  );

  // Reactive: writeFirstRun({briefSubmitted:true}) re-renders useUser and
  // flips the gate off without any navigation.
  const briefSubmitted = readFirstRun(user).briefSubmitted === true;
  const skip = !user || briefSubmitted;

  // null = unknown (probe not resolved) → render children, never a flash of
  // the funnel for paid/org users.
  const [trialLocal, setTrialLocal] = useState<boolean | null>(null);
  const probesRef = useRef(0);

  useEffect(() => {
    if (skip || trialLocal !== null) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const probe = async () => {
      try {
        const account = (await api.get("/billing/account")) as {
          subscription_status?: string | null;
        } | null;
        if (cancelled) return;
        setTrialLocal(account?.subscription_status === "trial_local");
      } catch (err) {
        if (cancelled) return;
        if (
          err instanceof ApiError &&
          err.status === 404 &&
          probesRef.current < MAX_PROBES
        ) {
          probesRef.current += 1;
          timer = setTimeout(() => void probe(), PROBE_MS);
          return;
        }
        setTrialLocal(false);
      }
    };
    void probe();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [skip, trialLocal, api]);

  if (!skip && trialLocal === true) return <FirstRunFunnel />;
  return <>{children}</>;
}
