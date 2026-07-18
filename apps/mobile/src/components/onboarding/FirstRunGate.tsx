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
 * `GET /container/status` (which ProvisionGate is polling in parallel) —
 * and `GET /billing/account` NEVER 404s: a row-less user gets 200 with
 * `subscription_status: null` (billing.py "pre-signup defaults"). So a
 * null status means "not yet" — re-probe on a provisioning-window cadence,
 * bounded so a genuinely row-less account eventually falls through to the
 * normal shell. Any non-null status is definitive either way; 403 (org
 * member) is definitive: not eligible.
 *
 * While the probe is unresolved the gate BLOCKS with a neutral loading
 * frame (web parity — ProvisioningStepper renders its own loading phase
 * while deciding): rendering the shell first would let a not-yet-briefed
 * trial user slip into chat past the no-skip funnel, and swapping the
 * funnel in a beat later flashes. The block is one round-trip for anyone
 * with a billing row; only the row-less fresh signup (who has no working
 * chat behind the gate anyway) waits through the null-status re-probes.
 * Probe FAILURES fail open immediately — an offline legacy user must not
 * stare at a loading frame for a retry window; eligibility simply
 * re-evaluates on the next mount.
 */
import { useAuth, useUser } from "@clerk/clerk-expo";
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Animated, StyleSheet, Text, View } from "react-native";
import { createApiClient } from "@isol8/shared";

import { usePulse } from "../chat/usePulse";
import { colors, fonts } from "../../config/theme";
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

  // null = unknown (probe not resolved) → block on the neutral loading
  // frame below: no funnel bypass, no funnel flash for eligible users.
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
        const status = account?.subscription_status ?? null;
        // Null status = pre-signup / trial row not minted yet (the endpoint
        // never 404s) — keep probing through the provisioning window.
        if (status === null && probesRef.current < MAX_PROBES) {
          probesRef.current += 1;
          timer = setTimeout(() => void probe(), PROBE_MS);
          return;
        }
        setTrialLocal(status === "trial_local");
      } catch {
        if (cancelled) return;
        // Fail open right away — 403 (org member) is definitively out, and
        // a launch-time network flake must not hold the loading frame
        // through a retry window; eligibility re-evaluates on next mount.
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
  // Unresolved probe: block with a neutral frame instead of the shell —
  // no funnel bypass, no funnel pop-in flash (see module docstring).
  if (!skip && trialLocal === null) return <LoadingFrame />;
  return <>{children}</>;
}

/** Pulsing "8" on cream — same idiom as ProvisionGate's interstitial. */
function LoadingFrame() {
  const opacity = usePulse(true, 0.35);
  return (
    <View style={styles.loadingPage}>
      <Animated.Text style={[styles.loadingGlyph, { opacity }]}>8</Animated.Text>
      <Text style={styles.loadingText}>Getting things ready…</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  loadingPage: {
    flex: 1,
    alignItems: "center",
    justifyContent: "center",
    gap: 8,
    backgroundColor: colors.cream,
  },
  loadingGlyph: {
    fontFamily: fonts.loraItalic,
    fontSize: 48,
    lineHeight: 56,
    color: colors.forest,
  },
  loadingText: {
    fontFamily: fonts.dmSans,
    fontSize: 13,
    lineHeight: 18,
    color: colors.mute,
  },
});
