"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";

import { capture } from "@/lib/analytics";
import {
  Loader2,
  XCircle,
  CheckCircle,
  Circle,
} from "lucide-react";
import { useOrganization, useAuth, useUser } from "@clerk/nextjs";
import { Button } from "@/components/ui/button";
import { useApi } from "@/lib/api";
import { readFirstRun } from "@/lib/firstRun";
import { FirstRunFunnel } from "@/components/onboarding/FirstRunFunnel";
import { useBilling } from "@/hooks/useBilling";
import { useProvisioningState, type ColdStartPhase, type BlockedPayload } from "@/hooks/useProvisioningState";
import { useGateway } from "@/hooks/useGateway";
import { useGatewayRpc } from "@/hooks/useGatewayRpc";
import { nextOnboardingCompletion } from "@/components/chat/onboardingAnalytics";
import { replaceSearchParams } from "@/lib/shellNav";
import { ChatGPTOAuthStep } from "@/components/chat/ChatGPTOAuthStep";
import { ByoKeyStep } from "@/components/chat/ByoKeyStep";
import { CreditsStep } from "@/components/chat/CreditsStep";
import { TeamBillingCard } from "@/components/chat/TeamBillingCard";
import { Logo } from "@/components/Logo";
import { PROVIDER_CARDS, type ProviderChoiceId } from "@/lib/providerCards";
// Provisioning copy + timing live in @isol8/shared so the web and mobile
// steppers stay in lockstep. Aliased to this file's historical local names so
// every usage site below is unchanged.
import {
  PROVISION_IDEAS,
  PROVISION_IDEA_ROTATION_MS as IDEA_ROTATION_MS,
  PROVISION_TIMEOUT_MS as TIMEOUT_MS,
  formatProvisionElapsed as formatElapsed,
} from "@isol8/shared";

// Frontend stepper phases. The cold-start "container", "gateway",
// "channels-sidecars", and "agent" steps all map to the same backend
// container.status="running" / gateway pool not-yet-connected window —
// the backend can only distinguish provisioning / starting / ready.
// We split that "starting" window into three visible UI steps via
// elapsed time so the user sees concrete progress during the 5+min
// upstream sidecars.channels phase instead of an unchanging spinner.
type Phase =
  | "payment"
  | "provider"
  | "container"
  | "gateway"
  | "channels-sidecars"
  | "agent"
  | "ready";

/**
 * Resolve the phase the stepper lands on once the gateway is up and the
 * "agent" cold-start step has cleared. Channel setup is no longer forced
 * before chat — it is nudged via the Get Started guide and remains
 * available in Settings → Channels — so this always resolves to "ready".
 */
export function resolvePostAgentPhase(): "ready" {
  return "ready";
}

import type { ProviderChoiceId as ProviderChoice } from "@/lib/providerCards";

function isProviderChoice(v: string | null): v is ProviderChoice {
  return v === "chatgpt_oauth" || v === "byo_key" || v === "bedrock_claude";
}

// After the provider step writes its credential (OAuth token / BYO key /
// credits), /container/status lags a few seconds before the gate evaluator
// observes it, so the gate keeps returning a 402 block. During that window we
// hold a calm "finishing" loader instead of the cold restart card, then reveal
// whatever block remains once the window lapses — so a genuine or unrelated
// block still surfaces. The status hook fast-polls (5s) while blocked, so the
// gate normally clears well within this window; the timeout is only a backstop.
const PROVIDER_SETTLE_MS = 30_000;

const PROVIDER_SETTLE_COPY: Record<ProviderChoice | "default", { title: string; desc: string }> = {
  chatgpt_oauth: {
    title: "Finishing ChatGPT sign-in",
    desc: "Connecting your ChatGPT account — this only takes a moment.",
  },
  byo_key: {
    title: "Saving your API key",
    desc: "Securing your key and starting your container.",
  },
  bedrock_claude: {
    title: "Setting up your credits",
    desc: "Getting Bedrock access ready and starting your container.",
  },
  default: {
    title: "Finishing setup",
    desc: "Just a moment while we get things ready.",
  },
};

const STEPS_PAID: { phase: Phase; label: string; activeLabel: string }[] = [
  { phase: "payment", label: "Payment confirmed", activeLabel: "Confirming payment..." },
  { phase: "container", label: "Container ready", activeLabel: "Provisioning your container..." },
  { phase: "gateway", label: "Gateway connected", activeLabel: "Connecting to your agent's brain..." },
  { phase: "channels-sidecars", label: "Channels wired up", activeLabel: "Wiring up your channels..." },
  { phase: "agent", label: "Agent online", activeLabel: "Putting the finishing touches on your agent..." },
  { phase: "ready", label: "Ready", activeLabel: "Ready!" },
];

// Full ordered phase list for ANALYTICS ONLY. Unlike STEPS_PAID (which drives
// the visible stepper rows and deliberately omits the interstitial "provider"
// phase), this includes every phase the machine can enter — so the
// `onboarding_step_entered` funnel can see the provider step. Without
// "provider" here it stays a measurement blind spot: users who abandon at the
// ChatGPT-OAuth / BYO-key / credits screen emit no forward `step_completed`
// event, so the drop-off was invisible.
const ONBOARDING_PHASE_ORDER: Phase[] = [
  "payment",
  "provider",
  "container",
  "gateway",
  "channels-sidecars",
  "agent",
  "ready",
];

// Persisted marker so the onboarding_ready activation milestone fires at most
// once per browser, ever. Routine restarts of a stopped/errored container also
// pass through provisioning on a normal /chat visit, so an in-memory guard
// alone can't distinguish a first activation from a replay.
const ONBOARDING_READY_STORAGE_KEY = "isol8_onboarding_ready_v1";

// Same idiom as ONBOARDING_READY_STORAGE_KEY above, for the free-tier
// `trial_started` funnel milestone — a distinct marker because a trial owner
// who later converts to paid must NOT re-fire this on a subsequent
// container recovery pass.
const TRIAL_STARTED_STORAGE_KEY = "isol8_trial_started_v1";

function RotatingIdeas({ eyebrow }: { eyebrow: string }) {
  const [idx, setIdx] = useState(0);
  const [visible, setVisible] = useState(true);

  useEffect(() => {
    if (typeof window !== "undefined" &&
        window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      return;
    }
    const tick = setInterval(() => {
      setVisible(false);
      const swap = setTimeout(() => {
        setIdx((i) => (i + 1) % PROVISION_IDEAS.length);
        setVisible(true);
      }, 180);
      return () => clearTimeout(swap);
    }, IDEA_ROTATION_MS);
    return () => clearInterval(tick);
  }, []);

  return (
    <>
      <span className="provision-idea-eyebrow">{eyebrow}</span>
      <span
        className={`provision-idea${visible ? "" : " provision-idea-fading"}`}
        aria-live="polite"
        aria-atomic="true"
      >
        {PROVISION_IDEAS[idx]}
      </span>
    </>
  );
}

export function ProvisioningStepper({
  children,
  trigger = "onboarding",
}: {
  children: React.ReactNode;
  /** "onboarding" = full flow (billing → container → gateway → channels → ready).
   *  "recovery" = skip billing, start from container provisioning. */
  trigger?: "onboarding" | "recovery";
}) {
  const { organization } = useOrganization();
  const isOrg = !!organization;
  const { userId: clerkUserId } = useAuth();
  // Clerk user object — the first-run funnel branch below reads (and its
  // component writes) user.unsafeMetadata.firstRun. `isLoaded` distinguishes
  // "Clerk still resolving" (hold a loader) from "loaded, signed out"
  // (user === null) — see the flash guard before the ready branch.
  const { user, isLoaded: userLoaded } = useUser();
  // Per-OWNER activation-marker key so a shared/QA browser doesn't let the first
  // account's activation suppress onboarding_ready for a later account's real
  // first cold start. The event is a person-level denominator (PostHog is
  // identified by Clerk user), so scope the once-ever marker to the owner
  // (org id for org workspaces, else the Clerk user id).
  const readyStorageKey = `${ONBOARDING_READY_STORAGE_KEY}:${organization?.id ?? clerkUserId ?? "anon"}`;
  // Same owner-scoping for the trial_started once-marker (see below).
  const trialStartedStorageKey = `${TRIAL_STARTED_STORAGE_KEY}:${organization?.id ?? clerkUserId ?? "anon"}`;
  const {
    account,
    isLoading: billingLoading,
    isSubscribed,
    refresh: refreshBilling,
    startTeamCheckout,
  } = useBilling();
  // Free-tier discriminator: trial_local accounts are already is_subscribed
  // (the backend added trial_local to _ACTIVE_STATUSES) but never set
  // provider_choice — there is no provider to pick for the free tier. Used
  // below to keep these users off the payment short-circuit, the poll gate,
  // and — the actually-broken case — the ProviderPicker fallback.
  const isTrialLocal = account?.subscription_status === "trial_local";
  // Set when we first enter the container/gateway phase — _not_ at component
  // mount. Time spent on the pre-provision steps (provider choice, billing
  // checkout, OAuth) shouldn't count against the timeout budget. A ref because
  // assigning it shouldn't trigger a re-render — the per-second tick effect
  // owns that via setElapsedMs.
  const provisioningStartRef = useRef<number | null>(null);
  const [timedOut, setTimedOut] = useState(false);
  const [elapsedMs, setElapsedMs] = useState(0);

  // Plan 3 (flat-fee pivot): `?provider=` is set by the landing page CTA
  // (/sign-up?provider=chatgpt_oauth|byo_key|bedrock_claude) and preserved
  // through Clerk's signup redirect to /onboarding -> /chat. When present,
  // we render a provider-specific step BEFORE container provisioning so the
  // user's provider_choice is on file when the gateway first looks at it
  // (Plan 3 Tasks 4 + 5 gate card-3 chats on credit balance).
  //
  // When `?provider=` is absent (e.g. the user navigated directly to
  // /onboarding, or the URL was scrubbed), the provider step is skipped
  // entirely and the legacy flow takes over. Plan 3 cutover (Task 16)
  // removes the legacy path.
  const searchParams = useSearchParams();
  const rawProvider = searchParams.get("provider");
  const providerChoice: ProviderChoice | null = isProviderChoice(rawProvider) ? rawProvider : null;
  const [providerStepDone, setProviderStepDone] = useState(false);
  const needsProviderStep = providerChoice !== null && !providerStepDone;

  // Once the provider step is complete, strip ?provider= from the URL so a
  // page reload doesn't re-enter the provider step. providerStepDone is a
  // local state that resets on remount; ?provider= persists in the URL
  // unless we clear it. For chatgpt_oauth specifically, re-entering the
  // step calls /oauth/chatgpt/start which 409s on an already-active
  // session — dead-ending the user.
  useEffect(() => {
    if (!providerStepDone) return;
    if (!searchParams.get("provider")) return;
    const params = new URLSearchParams(searchParams.toString());
    params.delete("provider");
    replaceSearchParams(params);
  }, [providerStepDone, searchParams]);

  // Which provider the user just finished in THIS session, captured at
  // completion so the "finishing" copy stays specific even after ?provider= is
  // stripped from the URL above. Null until the step completes; resets on remount.
  const [completedProvider, setCompletedProvider] = useState<ProviderChoice | null>(null);

  // Grace window: while it is open and the gate is still blocked, the settling
  // loader below wins over the cold gate card; once it lapses the real gate
  // card takes over again. The window is OPENED synchronously in
  // handleProviderComplete (so providerSettling lands in the same commit as
  // providerStepDone — otherwise this effect, which runs post-commit, would lag
  // one frame and flash the main stepper). This effect only schedules the
  // backstop that closes the window.
  const [providerSettling, setProviderSettling] = useState(false);
  useEffect(() => {
    if (!providerStepDone) return;
    const timer = setTimeout(() => setProviderSettling(false), PROVIDER_SETTLE_MS);
    return () => clearTimeout(timer);
  }, [providerStepDone]);

  // Whether the canonical /container/status poller is running, mirrored into
  // state so SWR's number-typed refreshInterval can consume it. The cadence we
  // want depends on the container THIS hook returns (a one-hook input<->output
  // cycle that can't be passed inline), so we converge this mirror during
  // render right after the hook — see shouldPollContainer below. Pure function
  // of the live phase: no effect, no lag, auto-re-arming.
  const [pollActive, setPollActive] = useState(true);

  // Single canonical /container/status poller. Gate-driven cadence
  // wins when blocked (5s/30s); cold-start polling at 3s otherwise so
  // the user sees fast progress through phase transitions.
  // No `enabled` gate here: an admin with a `past_due` / `canceled` /
  // `unpaid` / `paused` billing row has `isSubscribed === false`, but
  // we still need /container/status to fire so the backend's 402
  // subscription_required / payment_past_due gate surfaces. The
  // refreshInterval below already throttles non-blocked polling for
  // unsubscribed users (interval=0 when !shouldPollContainer); the
  // gate-driven cadence (5s/30s) still wins for pending states. An
  // `enabled: isSubscribed` gate would bypass the entire fetch for
  // inactive subscriptions and lose the /settings/billing reactivation
  // CTA.
  const {
    phase: gatePhase,
    blocked,
    container,
    refresh,
  } = useProvisioningState({
    refreshInterval: pollActive ? 3000 : 0,
    // Self-heal latched/backgrounded tabs: an offline->online or
    // background->foreground focus revalidates /container/status, so a tab
    // that missed the instant-ready WS push (or regressed while hidden)
    // recovers on the next focus instead of freezing. Only THIS call site
    // opts in — the hook default stays false so other consumers
    // (useSystemHealth etc.) keep their own chosen semantics. All instances
    // share the SWR key, so this one revalidating instance heals every reader.
    revalidateOnFocus: true,
  });

  // Poll /container/status while the CURRENT phase is pre-ready. Derived, not
  // latched: an earlier one-way `reachedReady` latch turned polling off forever
  // after the first ready — so a later regression (reprovision / wipe /
  // scale-to-zero) rendered the cold-start UI with polling permanently dead
  // (verified prod incident: frozen 15+ min over a ready backend). Deriving
  // from the live phase re-arms automatically on regression and stops at
  // ready/parked (the WS is the live channel there). `container == null`
  // (first mount, transient blips) counts as pre-ready: polling is the only
  // way out of that state, and a few extra requests over live chat during a
  // blip are harmless.
  const backendPhaseNow = container?.phase;
  const shouldPollContainer =
    (isSubscribed || isTrialLocal) &&
    backendPhaseNow !== "ready" &&
    backendPhaseNow !== "parked";
  // Converge the poll-active mirror during render (React's adjust-state-in-
  // render — the same pattern useProvisioningState uses internally). Zero-lag:
  // the hook re-runs synchronously with the new interval before commit, so
  // polling re-arms in the very render the regression lands — no dead-poll gap.
  if (pollActive !== shouldPollContainer) setPollActive(shouldPollContainer);

  // Instant-ready: the backend's connection pool pushes
  // status_change("HEALTHY") over the WebSocket at the exact moment the
  // container handshake completes. Revalidate /container/status right then
  // so the stepper flips to ready in one round trip instead of waiting out
  // the remainder of the 3s poll interval.
  const { onEvent } = useGateway();
  useEffect(() => {
    return onEvent((event, payload) => {
      if (event !== "status_change") return;
      if ((payload as { state?: string } | null)?.state !== "HEALTHY") return;
      refresh();
    });
  }, [onEvent, refresh]);

  // The trial_local row is created as a side effect of /container/status, not
  // by /billing/account — and useBilling does not revalidate on focus or an
  // interval. So when a container is provisioning but billing still shows no
  // active subscription/trial, pull /billing/account once so isTrialLocal /
  // isSubscribed reflect the just-created trial — which resumes cold-start
  // polling (shouldPollContainer) and the trial framing/telemetry.
  // Self-terminating: once billing reports the trial the condition is false.
  useEffect(() => {
    if (container && !isSubscribed && !isTrialLocal && !billingLoading) {
      refreshBilling();
    }
  }, [container, isSubscribed, isTrialLocal, billingLoading, refreshBilling]);

  // Provisioning is now driven by /container/status: when the gate clears
  // and no container exists, the backend's status endpoint fires
  // ensure_user_ready itself. The frontend stepper is purely passive — it
  // polls /container/status (every 3s during pre-ready) and renders the
  // appropriate phase. No explicit POST /container/provision from here.

  const containerReady = container?.status === "running" || container?.substatus === "gateway_healthy";

  // Honest /container/status error contract (all additive): status="error" is
  // a REAL failure — previously masked as "provisioning" so a dead container
  // spun a stepper forever. The UI keys off `substatus`, NEVER `phase` (which
  // still resolves to a cold-start value like "container" for an error row, so
  // it could never tell a dead container from a live one).
  //
  // Error is TERMINAL by DEFAULT — `error_retrying` is the ONLY non-terminal
  // exception. Keying "terminal" off a specific substatus (e.g. only
  // `provision_failed`) is too narrow: the backend also emits terminal rows
  // with `deployment_failed` (rollout circuit breaker) and a bare
  // `status:"error"` with NO substatus (auto-retry gated / standing down), and
  // those would fall through to the plain spinner with no Retry/Support CTA —
  // the exact endless-spinner bug this contract exists to kill. Treating any
  // non-retrying error as terminal restores the old catch-all's coverage and
  // absorbs any future terminal substatus. `last_error` carries the reason.
  const isRetryingError =
    container?.status === "error" && container?.substatus === "error_retrying";
  const isTerminalError =
    container?.status === "error" && !isRetryingError;

  // Poll gateway health every 3s once container looks ready
  const { data: gatewayHealth } = useGatewayRpc<Record<string, unknown>>(
    shouldPollContainer && containerReady ? "health" : null,
    undefined,
    { refreshInterval: 3000, dedupingInterval: 2000 },
  );

  // Track previous backend phase via a ref so we can detect regression
  // edges *during* render. When the backend goes ready -> not-ready
  // (e.g., scale-to-zero brought it back to provisioning) the
  // elapsedMs state is still the stale value from the prior cold-start
  // session — the reset effect below clears it but only after this
  // render commits. Without this regression detection, the stepper
  // would flash "agent" / "timed out" for a single frame before
  // settling onto the correct early sub-phase.
  const previousBackendPhaseRef = useRef<ColdStartPhase | undefined>(undefined);
  const isRegressingFromReady =
    // eslint-disable-next-line react-hooks/refs
    (previousBackendPhaseRef.current === "ready" || previousBackendPhaseRef.current === "parked") &&
    container?.phase !== undefined &&
    container.phase !== "ready" &&
    // Parked is ready-equivalent: a healthy box the backend put to sleep,
    // one intent away from running. Treating ready->parked (or parked
    // holding steady) as a regression would swap the whole chat surface
    // for a cold-start stepper over a perfectly readable page.
    container.phase !== "parked";
  // Mutate the ref during render — refs are React-blessed for this
  // pattern (derived state from prop transitions).
  // eslint-disable-next-line react-hooks/refs
  previousBackendPhaseRef.current = container?.phase;

  // Derive phase purely from data. The cold-start window (backend
  // phase="starting", roughly ECS RUNNING through gateway-pool-connected)
  // is split into three visible UI sub-phases by elapsed time so the
  // user sees concrete progress through what is currently a 5+min wait
  // in upstream's sidecars.channels.
  const phase: Phase = useMemo(() => {
    // Just regressed from ready -> not-ready? Force the conservative
    // early step until the reset effect clears stale timing state.
    if (isRegressingFromReady) return "container";
    // Pre-signup user lands in payment phase to pick a provider path.
    // Recovery flow skips billing — the user already has a subscription. A
    // provisioning container (200 from /container/status) means the backend
    // already gated this user in — a fresh personal signup gets an
    // auto-created trial_local there — so don't force payment while the
    // container is coming up, even if billing hasn't caught up to the trial.
    if (trigger !== "recovery" && !isSubscribed && !isTrialLocal && !container) return "payment";
    // Provider-specific signup step runs before container provision.
    if (needsProviderStep) return "provider";

    // Trust the backend's phase field when it's present; fall back to
    // the legacy status check for older backends still in flight.
    const backendPhase = container?.phase;

    if (!container) return "container";
    // Parked (wake-gated trial box, deliberately asleep): render the app —
    // content stays readable, and any interaction / message send wakes it.
    // Checked BEFORE the error/status branches: a parked row's status is
    // "stopped", which the legacy checks would misread as a cold start.
    if (backendPhase === "parked") return resolvePostAgentPhase();
    if (container.status === "error") return "container";
    if (backendPhase === "provisioning") {
      // substatus "task_running" = the ECS task is RUNNING with an IP but
      // the gateway port hasn't opened yet (image pulled, OpenClaw
      // booting) — the real "gateway" window.
      return container.substatus === "task_running" ? "gateway" : "container";
    }
    if (backendPhase === undefined && !containerReady) return "container";

    if (backendPhase === "starting" || (backendPhase === undefined && !gatewayHealth)) {
      // phase="starting" means the gateway port answered the TCP probe:
      // the gateway is up and OpenClaw is loading channels/sidecars.
      // Once the health RPC answers, only the pool handshake remains —
      // the "agent" step. Every transition here is a real signal.
      return gatewayHealth ? "agent" : "channels-sidecars";
    }

    // backend phase="ready" (or legacy: gatewayHealth present) — chat is
    // ready. Channel setup is no longer forced here; it is nudged via the
    // Get Started guide and lives in Settings → Channels.
    return resolvePostAgentPhase();
  }, [trigger, isSubscribed, isTrialLocal, container, containerReady, gatewayHealth, needsProviderStep, isRegressingFromReady]);

  // Analytics: fire `onboarding_step_completed` (step LEFT) + `onboarding_step_entered`
  // (step ARRIVED) per phase transition.
  //
  // `step_completed` only fires on forward moves through STEPS_PAID, so a user
  // who STALLS on a step emits nothing, and the "provider" phase (absent from
  // STEPS_PAID) never appears at all — the two blind spots that made onboarding
  // drop-off unmeasurable. `step_entered` fixes both: it fires on entry to every
  // phase (including "provider", indexed via ONBOARDING_PHASE_ORDER), so a stall
  // is now visible as "entered X, never entered X+1", and provider abandonment
  // becomes a real funnel step. Entry to the provider phase also carries the
  // chosen provider so we can segment OAuth vs BYO vs credits drop-off.
  const prevPhaseRef = useRef<Phase | null>(null);
  const readyFiredRef = useRef(false);
  // Guard `onboarding_ready` against false activations. Two failure modes:
  // (1) an already-provisioned user opening /chat has no container data on first
  // render, so the UI briefly derives phase="container" then jumps to "ready"
  // with no real cold start; (2) an existing user whose container was
  // stopped/errored gets auto-restarted on /chat and passes through provisioning
  // AGAIN — a routine restart, still trigger="onboarding". Both would inflate the
  // activation denominator + TTFV. So we require BOTH a witnessed cold start
  // (backend phase went through provisioning/starting — rules out #1) AND a
  // persisted once-ever marker (rules out #2 and every future replay): this is a
  // first-activation milestone, so it must fire at most once per browser, ever.
  const sawColdStartRef = useRef(false);
  useEffect(() => {
    if (container?.phase === "provisioning" || container?.phase === "starting") {
      sawColdStartRef.current = true;
    }
  }, [container?.phase]);
  useEffect(() => {
    const prev = prevPhaseRef.current;
    prevPhaseRef.current = phase;
    if (prev === phase) return;
    capture("onboarding_step_entered", {
      step_name: phase,
      step_index: ONBOARDING_PHASE_ORDER.indexOf(phase),
      ...(phase === "provider" && providerChoice !== null
        ? { provider_choice: providerChoice }
        : {}),
    });
    // Activation milestone: the first time a container reaches "ready" after a
    // WITNESSED cold start, record it with the real duration (from
    // provisioningStartRef). This is the activation DENOMINATOR + time-to-first-
    // value the step_completed funnel tail can't give (fast phase-skips drop
    // intermediate events, undercounting "reached ready" ~3x).
    //
    // The once-ever marker is written on ANY first observed "ready" — including
    // a pre-existing user already ready with no cold start this visit — so a
    // later routine restart (stopped/errored container auto-restarts and
    // re-enters provisioning) can never be miscounted as a new activation. Only
    // the witnessed-cold-start case CAPTURES; an already-ready load just
    // backfills the marker.
    if (phase === "ready" && !readyFiredRef.current) {
      readyFiredRef.current = true;
      let alreadyActivated = false;
      try {
        alreadyActivated = window.localStorage.getItem(readyStorageKey) === "1";
      } catch {
        // storage-disabled browser: fall back to the in-mount ref guard only
      }
      if (!alreadyActivated) {
        try {
          window.localStorage.setItem(readyStorageKey, "1");
        } catch {
          /* best-effort */
        }
        // Capture a REAL activation only on the onboarding trigger. A
        // "recovery" restart (an existing user manually recovering a stopped
        // container) also cold-starts, and on a fresh/cleared browser (or with
        // storage disabled) the marker above wouldn't yet exist to suppress it —
        // so the trigger check is the third guard alongside witnessed-cold-start
        // and the once-ever marker.
        if (sawColdStartRef.current && trigger === "onboarding") {
          const start = provisioningStartRef.current;
          capture("onboarding_ready", {
            elapsed_ms: start !== null ? Date.now() - start : null,
          });
        }
      }
    }
    if (prev === null) return;
    const completion = nextOnboardingCompletion(prev, phase, STEPS_PAID);
    if (completion) capture("onboarding_step_completed", { ...completion });
  }, [phase, providerChoice, readyStorageKey]);

  // `trial_started`: the free-tier equivalent of an activation milestone.
  // trial_local accounts skip payment/provider entirely (see the `phase`
  // memo above), so there's no natural "step_completed" transition to hang
  // this off of — it has to be driven directly off `isTrialLocal` becoming
  // true. Fires on the RISING EDGE only (false -> true), guarded by the same
  // owner-scoped once-ever localStorage marker idiom as `onboarding_ready`
  // above, so a returning trial user re-opening /chat doesn't re-fire it.
  const prevIsTrialLocalRef = useRef(false);
  useEffect(() => {
    const wasTrialLocal = prevIsTrialLocalRef.current;
    prevIsTrialLocalRef.current = isTrialLocal;
    if (!isTrialLocal || wasTrialLocal) return;
    // A "recovery" restart (existing trial owner's stopped container
    // auto-restarting) also re-mounts this component, resetting the ref
    // above to `false` and thus looking like a rising edge again. Same
    // reasoning as the onboarding_ready trigger guard: only the "onboarding"
    // trigger represents a genuine (first) trial start.
    if (trigger !== "onboarding") return;
    let alreadyStarted = false;
    try {
      alreadyStarted = window.localStorage.getItem(trialStartedStorageKey) === "1";
    } catch {
      // storage-disabled browser: best-effort only, may re-fire on reload
    }
    if (alreadyStarted) return;
    try {
      window.localStorage.setItem(trialStartedStorageKey, "1");
    } catch {
      /* best-effort */
    }
    capture("trial_started");
  }, [isTrialLocal, trigger, trialStartedStorageKey]);

  // Record provision-gate blocks (subscription_required / oauth-missing /
  // credits_required / signup_required) keyed to the PERSON, once per code
  // per mount. The server only emits an EMF metric with no person_id, so
  // without this we can't tell WHICH users stalled at a gate, split by
  // reason, or whether the same users ever recovered/converted.
  //
  // The signup gate is a special case: /container/status returns it as a 404,
  // which useProvisioningState surfaces as gatePhase="needs-signup" with
  // blocked=null (NOT the 402 "blocked" payload). Handle it explicitly with a
  // synthetic "signup_required" code — it is the LARGEST gate bucket
  // (ProviderPicker/no-subscription abandonment), so missing it would blind the
  // signal to the most common stall.
  const gateBlockFiredRef = useRef<string | null>(null);
  useEffect(() => {
    let code: string | null = null;
    let ownerRole: BlockedPayload["owner_role"] | undefined;
    if (gatePhase === "blocked" && blocked) {
      code = blocked.code;
      ownerRole = blocked.owner_role;
    } else if (gatePhase === "needs-signup") {
      code = "signup_required";
    }
    if (code && gateBlockFiredRef.current !== code) {
      gateBlockFiredRef.current = code;
      capture("onboarding_gate_blocked", {
        code,
        ...(ownerRole ? { owner_role: ownerRole } : {}),
      });
    }
  }, [gatePhase, blocked]);

  // Tick elapsed once per second so the user sees real progress against the
  // expected budget — gives "still working" instead of an indefinite spinner.
  // Also stamps the start time the first time we enter container/gateway and
  // flips `timedOut` when we cross TIMEOUT_MS, which softens the copy.
  // Reset cold-start timing state when the user re-enters a pre-ready
  // phase (e.g., scale-to-zero brought the container back to
  // provisioning, or a recovery path re-fired). Without this, the
  // user sees stale elapsedMs and a stuck timedOut banner from a
  // previous run.
  const wasPreviouslyAdvancedRef = useRef(false);
  useEffect(() => {
    const isColdStartPhase =
      phase === "container" ||
      phase === "gateway" ||
      phase === "channels-sidecars" ||
      phase === "agent";
    if (!isColdStartPhase) {
      wasPreviouslyAdvancedRef.current = phase === "ready";
      return;
    }
    // Only reset when re-entering after having previously left this
    // window — first-time entry handled by the elapsed-tick effect below.
    if (wasPreviouslyAdvancedRef.current) {
      provisioningStartRef.current = null;
      setElapsedMs(0);
      setTimedOut(false);
      wasPreviouslyAdvancedRef.current = false;
    }
  }, [phase]);

  // Poll watchdog. SWR drives /container/status via a SELF-RESCHEDULING
  // refreshInterval chain: each revalidation schedules the next only after it
  // resolves. When Chrome throttles a backgrounded OR merely occlusion-throttled
  // tab, a single dropped tick ends that chain permanently — the configured 3s
  // cadence simply stops firing and the tab makes near-zero /container/status
  // fetches. revalidateOnFocus can't heal a VISIBLE-but-throttled tab either: it
  // never gets a focus TRANSITION to fire on. Verified live on dev, twice: a
  // throttled-but-visible tab managed ~1 status poll in 3+ minutes, funnel and
  // stepper frozen on stale data while the backend already answered
  // onboarded/ready. The elapsed timer's 1s setInterval below provably SURVIVES
  // that throttling — its on-screen counter kept counting through the freeze —
  // so we ride it: fire refresh() at most once / 10s while a pre-ready cold-start
  // card is displayed. It is a SEPARATE timer issuing an out-of-band mutate(), so
  // it is not subject to the dead SWR chain — the only self-heal that reaches a
  // visible-but-throttled tab.
  //
  // Cost: during a HEALTHY cold start the live 3s poll is still running and SWR's
  // dedupingInterval collapses most overlaps, so this adds ~1 extra request / 10s
  // against a cheap endpoint for at most a few minutes; once the chain is
  // throttle-killed the watchdog is the ONLY thing still asking. Seeded to mount
  // time inside the effect below (not here) so Date.now() is never called during
  // render; first fire lands ~10s in, never an immediate double-fetch on the
  // first poll.
  const lastWatchdogRef = useRef<number | null>(null);

  useEffect(() => {
    if (
      phase !== "container" &&
      phase !== "gateway" &&
      phase !== "channels-sidecars" &&
      phase !== "agent"
    ) {
      return;
    }
    if (provisioningStartRef.current === null) {
      provisioningStartRef.current = Date.now();
    }
    // Seed the watchdog clock on first cold-start entry (kept out of render to
    // stay pure). Deliberately NOT reset on sub-phase transitions, so the 10s
    // cadence holds across container -> gateway -> channels-sidecars -> agent.
    if (lastWatchdogRef.current === null) {
      lastWatchdogRef.current = Date.now();
    }
    const tick = () => {
      const start = provisioningStartRef.current;
      if (start === null) return;
      const ms = Date.now() - start;
      setElapsedMs(ms);
      if (ms > TIMEOUT_MS) setTimedOut(true);
      // Poll watchdog (see lastWatchdogRef above), throttled to once / 10s. The
      // phase guard already scopes us to the pre-ready cold-start cards; we
      // further require a normal gate — never re-poll a blocked / team-pending /
      // needs-signup state, which owns the hook's 5s/30s cadence and whose 30s
      // backoff a 10s re-bump would defeat — and a non-terminal container (the
      // Setup-failed card is not a live cold start).
      const lastWatchdog = lastWatchdogRef.current;
      if (
        lastWatchdog !== null &&
        gatePhase === "normal" &&
        !isTerminalError &&
        Date.now() - lastWatchdog > 10_000
      ) {
        lastWatchdogRef.current = Date.now();
        refresh();
      }
    };
    tick();
    const interval = setInterval(tick, 1000);
    return () => clearInterval(interval);
  }, [phase, gatePhase, isTerminalError, refresh]);

  // First-run funnel: whoever first reaches a ready company — trial_local
  // free-tier signup or an org owner/member — describes the business here
  // before reaching chat. It's the committed onboarding front door (no
  // skip), so the only way past it is submitting the brief (the backend's
  // per-company DynamoDB claim makes a second submitter within the same
  // org a no-op "already_submitted" rather than a duplicate). Placed
  // BEFORE the ready return so a quick provision can't drop the funnel and
  // lose the in-progress brief (and leave the kickoff hook, which requires
  // briefSubmitted, unable to fire). Gated on gatePhase === "normal" so it
  // never preempts a blocked / needs-signup / team-pending state. The
  // `user` guard is load-bearing beyond null-safety: submit writes
  // user.unsafeMetadata, so without a loaded Clerk user the funnel would
  // be a dead-end form.
  //
  // "Have we onboarded?" is a PER-CONTAINER fact, not per-user: an org
  // shares one container, so onboarding happens once for the whole org. The
  // backend stamps it on the container row and surfaces it as
  // `container.onboarded` on /container/status (already polled above), so
  // once anyone on this container completes the funnel, no one on it sees
  // the funnel again — including a second org member whose own per-user
  // marker is empty. No extra request: it rides on the poll we already do.
  // `container.onboarded` is the SOLE durable authority for "have we
  // onboarded?": the backend resets it when a container is wiped/reprovisioned,
  // so a legitimately reprovisioned owner re-onboards. `firstRun.briefSubmitted`
  // is DURABLE Clerk metadata (`user.unsafeMetadata`) that survives a container
  // wipe and that nothing ever clears — so reading it as a gate suppressed the
  // funnel FOREVER for any owner who onboarded once: a returning owner whose
  // container was reprovisioned has `container.onboarded === false` (fresh) but
  // a stale `briefSubmitted === true`, and the funnel never showed again. That
  // is the "returning owner never re-onboards" bug; `briefSubmitted` is NOT a
  // durable suppressor.
  //
  // It survives here only as a SAME-SESSION flash-bridge: when the funnel's
  // submit writes `briefSubmitted: true`, `useUser` re-renders and we arm an
  // in-memory latch on that false->true edge, dismissing the funnel at once
  // instead of leaving it up until the next /container/status poll flips
  // `onboarded` true. `briefBaselineRef` captures the flag's value at the first
  // loaded Clerk user, so the async user-load (undefined -> loaded) is never
  // mistaken for a submit and a stale `true` already present at mount does NOT
  // arm the latch — the funnel shows for a reprovisioned container, and
  // `container.onboarded` reasserts durable suppression on the next poll. The
  // refs are read/written during render (not via an effect, which would land a
  // commit late and flash the funnel for one frame) — same idiom as
  // `previousBackendPhaseRef` above.
  const firstRun = readFirstRun(user);
  const containerOnboarded = container?.onboarded === true;
  const briefSubmittedNow = !!user && firstRun.briefSubmitted === true;
  const briefBaselineRef = useRef<boolean | null>(null);
  const sessionBriefSubmittedRef = useRef(false);
  // eslint-disable-next-line react-hooks/refs
  if (user && briefBaselineRef.current === null) {
    briefBaselineRef.current = briefSubmittedNow;
    // eslint-disable-next-line react-hooks/refs
  } else if (briefSubmittedNow && briefBaselineRef.current === false) {
    // eslint-disable-next-line react-hooks/refs
    sessionBriefSubmittedRef.current = true;
  }
  if (
    !!user &&
    gatePhase === "normal" &&
    // A terminally-failed container must fall through to the Setup-failed card
    // below, never the funnel — submitting a brief to a dead backend is a
    // dead end. (A retrying error is NOT terminal, so it still shows the
    // funnel/provisioning UI like any live cold start.)
    !isTerminalError &&
    !containerOnboarded &&
    // eslint-disable-next-line react-hooks/refs
    !sessionBriefSubmittedRef.current
  ) {
    return <FirstRunFunnel />;
  }

  // Hold a neutral loader until Clerk resolves `user`. The funnel branch above
  // requires a loaded user, so a not-yet-onboarded container would otherwise
  // flash `children` (or the stepper) for a frame before the funnel takes over
  // once `user` loads. Only matters where the funnel COULD show — a
  // normal-gate, not-yet-onboarded, non-terminal container. A loaded-but-
  // signed-out user (isLoaded true, user null) is NOT a loading state and
  // falls straight through; a terminal error still routes to its card below.
  if (!userLoaded && gatePhase === "normal" && !containerOnboarded && !isTerminalError) {
    return (
      <div className="flex-1 flex items-center justify-center">
        <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
      </div>
    );
  }

  // Ready — render children. `teams_status: "failed"` is a TOP-LEVEL,
  // non-blocking signal on the honest contract: Teams (Friday/Atlas) seeding
  // failed but the container itself is healthy and chat works — so we render
  // the app as normal and layer a small dismissible "Teams needs attention"
  // notice over it, never a blocking interstitial.
  if (phase === "ready") {
    return (
      <>
        {children}
        {container?.teams_status === "failed" && <TeamsDegradedNotice />}
      </>
    );
  }

  // Team-pending: org member whose admin hasn't provisioned the shared
  // workspace yet. Backend Task 8 returns 202 from /container/status for
  // this case; useProvisioningState surfaces it as `phase === "team-pending"`.
  // Reuse the blocked-state centerpiece visuals (key icon, refresh button)
  // and swap the copy for "waiting on your admin". The hook keeps polling
  // at 5s so the UI flips to `normal` automatically once the admin's
  // container appears.
  if (gatePhase === "team-pending") {
    return (
      <>
        <style>{`
          .provision-wrapper {
            flex: 1;
            display: flex;
            align-items: center;
            justify-content: center;
            background: #faf7f2;
          }
          .provision-card {
            text-align: center;
            max-width: 400px;
            width: 100%;
            padding: 48px 32px;
          }
          .provision-logo {
            margin-bottom: 32px;
          }
          .provision-stage {
            width: 80px;
            height: 80px;
            margin: 0 auto 32px;
            position: relative;
          }
          .provision-anim {
            position: absolute;
            inset: 0;
            display: flex;
            align-items: center;
            justify-content: center;
          }
          .anim-key-circle {
            width: 56px; height: 56px;
            border: 2px solid #e0dbd0;
            border-top-color: #06402B;
            border-radius: 50%;
          }
          .anim-key-icon {
            position: absolute;
            inset: 0;
            display: flex;
            align-items: center;
            justify-content: center;
          }
          .provision-title {
            font-family: var(--font-lora-serif), serif;
            font-size: 22px; font-weight: 400; color: #1a1a1a;
            margin-bottom: 8px;
          }
          .provision-desc {
            font-family: var(--font-dm-sans), sans-serif;
            font-size: 14px; color: #8a8578;
            margin-bottom: 24px;
          }
          .provision-muted {
            font-family: var(--font-dm-sans), sans-serif;
            font-size: 13px;
            color: #8a8578;
            margin: 0;
          }
          .provision-timeout {
            margin-top: 28px;
            padding-top: 20px;
            border-top: 1px solid #ece6d9;
            display: flex; flex-direction: column;
            align-items: center; gap: 14px;
            max-width: 360px;
            margin-left: auto; margin-right: auto;
          }
          .provision-timeout-actions {
            display: flex; gap: 8px; align-items: center;
          }
        `}</style>
        <div className="provision-wrapper">
          <div className="provision-card">
            <div className="provision-logo">
              <Logo size="lg" href={null} />
            </div>

            <div className="provision-stage" aria-hidden="true">
              <div className="provision-anim">
                <div className="anim-key-circle" />
                <div className="anim-key-icon">
                  <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="#06402B" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                    <path d="M21 2l-2 2m-7.61 7.61a5.5 5.5 0 1 1-7.78 7.78 5.5 5.5 0 0 1 7.78-7.78zm0 0L15.5 7.5m0 0l3 3L22 7l-3-3m-3.5 3.5L19 4" />
                  </svg>
                </div>
              </div>
            </div>

            <h2
              className="provision-title"
              role="status"
              aria-live="polite"
              aria-atomic="true"
            >
              Your team workspace is being set up
            </h2>
            <p className="provision-desc">
              Your admin will finish this soon. We&apos;ll bring you in
              automatically once the workspace is ready.
            </p>

            <div className="provision-timeout">
              <div className="provision-timeout-actions">
                <Button
                  variant="outline"
                  size="sm"
                  className="rounded-full border-[#e0dbd0]"
                  onClick={() => refresh()}
                >
                  Check again
                </Button>
                <Button variant="ghost" size="sm" asChild>
                  <a
                    href="mailto:support@isol8.co"
                    onClick={() => capture("support_contact_clicked", { source: "onboarding_stepper", phase })}
                  >Contact support</a>
                </Button>
              </div>
            </div>
          </div>
        </div>
      </>
    );
  }

  // Needs-signup: backend signals via 404 that the owner has never
  // started signup (gate would fire `signup_required` for an admin).
  // Render the ProviderPicker by intent here — the picker's
  // POST /billing/trial-checkout is the only path that creates the
  // first billing row. Placed before billing/onboarded fall-through so
  // we don't depend on the post-billing-load render order to surface
  // the picker for fresh users.
  if (gatePhase === "needs-signup") {
    return <ProviderPicker isOrg={isOrg} orgName={organization?.name} />;
  }

  // Provider-specific signup step. Renders the right component based on
  // `?provider=` and advances the wizard once the user finishes.
  //
  // provider_choice is NOT persisted via /users/sync — that endpoint accepts
  // the field and ignores it. billing_accounts (owner-keyed) is the source of
  // truth, and it has exactly two writers: the ProviderPicker's "Subscribe"
  // step (POST /billing/trial-checkout, at signup) and POST /billing/provider
  // (post-signup switches — how ByoKeyStep commits its sub-provider, since
  // trial-checkout submits `byo_key` with no byo_provider).
  if (phase === "provider" && providerChoice !== null) {
    const handleProviderComplete = async () => {
      capture("onboarding_provider_completed", {
        provider_choice: providerChoice,
      });
      // Open the settling window in the same commit as providerStepDone so the
      // loader is shown on the very next render with no one-frame stepper flash.
      setCompletedProvider(providerChoice);
      setProviderSettling(true);
      setProviderStepDone(true);
    };
    return (
      <div className="flex-1 flex items-center justify-center p-6 bg-[#faf7f2]">
        <div className="w-full max-w-md">
          {providerChoice === "chatgpt_oauth" && (
            <ChatGPTOAuthStep onComplete={handleProviderComplete} />
          )}
          {providerChoice === "byo_key" && (
            <ByoKeyStep onComplete={handleProviderComplete} />
          )}
          {providerChoice === "bedrock_claude" && (
            <CreditsStep onComplete={handleProviderComplete} />
          )}
        </div>
      </div>
    );
  }

  // Provision-gate blocked state: backend returned 402 + structured payload
  // (e.g. credits_required, subscription_required). Render the same centerpiece
  // shell as the cold-start view (logo + key-stage) but swap the title, body,
  // and footer for the gate-specific copy + action. Placed AFTER the provider
  // step so that when the user clicks "Sign in with ChatGPT" (which sets
  // ?provider=chatgpt_oauth in the URL), the provider step wins and the gate UI
  // does not re-render on top of the OAuth flow. A member of an unsubscribed org
  // (no ?provider= in URL) still falls through to here and sees "Ask your org
  // admin" because the provider block's providerChoice guard fails first.
  //
  // Additional `providerChoice === null` guard: after the provider step
  // completes (`setProviderStepDone(true)`), `phase` flips OUT of "provider"
  // and the early-return above stops firing. But the URL still has
  // `?provider=chatgpt_oauth` until the strip-effect runs one frame later.
  // During the eventual-consistency window between the OAuth row write
  // (from /poll) and the next /container/status read by useProvisioningState,
  // the gate evaluator's `_has_oauth_tokens` check still returns false and
  // the gate would re-render the green "Sign in with ChatGPT" button on top
  // of what should be the provisioning loader. Suppressing the gate UI
  // entirely while `?provider=` is set closes that race.
  //
  // That URL-param guard only covers the one frame before the strip-effect
  // runs; the gate's eventual-consistency lag is several seconds. Once
  // ?provider= is stripped (providerChoice === null) while the gate is still
  // catching up, the cold "sign in again" restart card below would flash —
  // telling a user who JUST completed the step to redo it. While the grace
  // window is open we instead hold a calm "finishing" loader; the status hook
  // fast-polls (5s) so the gate clears itself, and after the window lapses the
  // real gate card takes over so a genuinely-stuck or unrelated block surfaces.
  if (gatePhase === "blocked" && blocked && providerStepDone && providerSettling) {
    const copy = PROVIDER_SETTLE_COPY[completedProvider ?? "default"];
    return (
      <div className="flex-1 flex items-center justify-center bg-[#faf7f2]">
        <div className="w-full max-w-[400px] px-8 py-12 text-center">
          <div className="mb-8 flex justify-center">
            <Logo size="lg" href={null} />
          </div>
          <div className="mx-auto mb-8 flex h-20 w-20 items-center justify-center">
            <Loader2 className="h-10 w-10 animate-spin text-[#06402B]" aria-hidden="true" />
          </div>
          <h2
            role="status"
            aria-live="polite"
            aria-atomic="true"
            className="mb-2 text-[22px] font-normal text-[#1a1a1a]"
            style={{ fontFamily: "var(--font-lora-serif), serif" }}
          >
            {copy.title}
          </h2>
          <p
            className="text-sm text-[#8a8578]"
            style={{ fontFamily: "var(--font-dm-sans), sans-serif" }}
          >
            {copy.desc}
          </p>
        </div>
      </div>
    );
  }

  // Teams billing paywall: the gate returns team_billing_required for an org
  // owner (or member) with no billing row. Render the real per-seat checkout
  // card instead of the generic blocked card. TeamBillingCard is admin-gated
  // internally — a member sees "ask your org admin" with no CTA.
  if (gatePhase === "blocked" && blocked && blocked.code === "team_billing_required") {
    return (
      <div className="flex-1 flex items-center justify-center bg-[#faf7f2]">
        <TeamBillingCard
          seatCount={account?.seat_count ?? null}
          seatPriceUsd={account?.team_seat_price_usd ?? null}
          ownerRole={blocked.owner_role}
          isSubscribed={false}
          onSubscribe={startTeamCheckout}
        />
      </div>
    );
  }

  if (gatePhase === "blocked" && blocked && providerChoice === null) {
    const showAction = !(blocked.action.admin_only && blocked.owner_role !== "admin");
    return (
      <>
        <style>{`
          .provision-wrapper {
            flex: 1;
            display: flex;
            align-items: center;
            justify-content: center;
            background: #faf7f2;
          }
          .provision-card {
            text-align: center;
            max-width: 400px;
            width: 100%;
            padding: 48px 32px;
          }
          .provision-logo {
            margin-bottom: 32px;
          }
          .provision-stage {
            width: 80px;
            height: 80px;
            margin: 0 auto 32px;
            position: relative;
          }
          .provision-anim {
            position: absolute;
            inset: 0;
            display: flex;
            align-items: center;
            justify-content: center;
          }
          .anim-key-circle {
            width: 56px; height: 56px;
            border: 2px solid #e0dbd0;
            border-top-color: #06402B;
            border-radius: 50%;
          }
          .anim-key-icon {
            position: absolute;
            inset: 0;
            display: flex;
            align-items: center;
            justify-content: center;
          }
          .provision-title {
            font-family: var(--font-lora-serif), serif;
            font-size: 22px; font-weight: 400; color: #1a1a1a;
            margin-bottom: 8px;
          }
          .provision-desc {
            font-family: var(--font-dm-sans), sans-serif;
            font-size: 14px; color: #8a8578;
            margin-bottom: 24px;
          }
          .provision-cta {
            display: inline-block;
            padding: 10px 22px;
            border-radius: 9999px;
            background: #06402B;
            color: #ffffff;
            font-family: var(--font-dm-sans), sans-serif;
            font-size: 14px;
            font-weight: 500;
            text-decoration: none;
          }
          .provision-cta:hover { background: #052e1f; }
          .provision-muted {
            font-family: var(--font-dm-sans), sans-serif;
            font-size: 13px;
            color: #8a8578;
            margin: 0;
          }
          .provision-timeout {
            margin-top: 28px;
            padding-top: 20px;
            border-top: 1px solid #ece6d9;
            display: flex; flex-direction: column;
            align-items: center; gap: 14px;
            max-width: 360px;
            margin-left: auto; margin-right: auto;
          }
          .provision-timeout-actions {
            display: flex; gap: 8px; align-items: center;
          }
        `}</style>
        <div className="provision-wrapper">
          <div className="provision-card">
            <div className="provision-logo">
              <Logo size="lg" href={null} />
            </div>

            {/* Animation stage — key icon (no spin), echoing the cold-start
                centerpiece so the visual layout stays consistent. */}
            <div className="provision-stage" aria-hidden="true">
              <div className="provision-anim">
                <div className="anim-key-circle" />
                <div className="anim-key-icon">
                  <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="#06402B" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                    <path d="M21 2l-2 2m-7.61 7.61a5.5 5.5 0 1 1-7.78 7.78 5.5 5.5 0 0 1 7.78-7.78zm0 0L15.5 7.5m0 0l3 3L22 7l-3-3m-3.5 3.5L19 4" />
                  </svg>
                </div>
              </div>
            </div>

            <h2
              className="provision-title"
              role="status"
              aria-live="polite"
              aria-atomic="true"
            >
              {blocked.title}
            </h2>
            <p className="provision-desc">{blocked.message}</p>

            {showAction ? (
              <a className="provision-cta" href={blocked.action.href}>
                {blocked.action.label}
              </a>
            ) : (
              <p className="provision-muted">Ask your org admin to fix this.</p>
            )}

            <div className="provision-timeout">
              <div className="provision-timeout-actions">
                <Button
                  variant="outline"
                  size="sm"
                  className="rounded-full border-[#e0dbd0]"
                  onClick={() => refresh()}
                >
                  Check again
                </Button>
                <Button variant="ghost" size="sm" asChild>
                  <a
                    href="mailto:support@isol8.co"
                    onClick={() => capture("support_contact_clicked", { source: "onboarding_stepper", phase })}
                  >Contact support</a>
                </Button>
              </div>
            </div>
          </div>
        </div>
      </>
    );
  }

  // Loading billing OR the first /container/status poll hasn't resolved yet.
  // Holding a neutral spinner over BOTH is what kills the provider-picker
  // flash for a brand-new trial signup: /billing/account returns null a beat
  // before the status poll creates the trial_local row and returns a
  // provisioning container, and in that gap the legacy fallback below
  // (!isTrialLocal && !container) would briefly render the paywall picker.
  // gatePhase === "loading" is the pre-first-response state, so waiting it
  // out means the next thing a new trial user sees is the funnel, not a
  // payment step they don't need.
  if (billingLoading || gatePhase === "loading") {
    return (
      <div className="flex-1 flex items-center justify-center">
        <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
      </div>
    );
  }

  // Pre-signup user — render the three signup paths inline. Each card links
  // to `/chat?provider=X`, which routes the wizard into the provider phase
  // below. The provider phase runs the chosen flow (OAuth / BYO key /
  // credits) and creates the trial subscription.
  //
  // Skip picker only when the owner is fully onboarded (has a provider
  // choice AND an active subscription). Two cases this handles:
  //
  //   1. Org member joining an already-onboarded org: admin already
  //      picked + subscribed, so the member skips the picker and falls
  //      through to provisioning.
  //
  //   2. User who picks then cancels Stripe Checkout: `provider_choice`
  //      is persisted before the Stripe redirect, so
  //      `account.provider_choice` is set but `isSubscribed` is false.
  //      They MUST see the picker again to restart checkout.
  //
  //   3. trial_local (free-tier) owner: is_subscribed is TRUE (backend
  //      treats trial_local as an active subscription) but provider_choice
  //      is never set — there's no provider to pick on the free tier. The
  //      `isTrialLocal` guard keeps these users off the picker so they flow
  //      straight into container/gateway/ready instead of being shown a
  //      payment step they don't need.
  //   4. A provisioning container (200 from /container/status) means the
  //      backend already gated this user in. For a fresh personal signup the
  //      trial_local row is created there, and /billing/account may not have
  //      caught up yet — so a live container bypasses the picker too, closing
  //      the cross-fetch race where a real trial user briefly reads as
  //      non-trial.
  const ownerOnboarded = isSubscribed && Boolean(account?.provider_choice);
  if (!ownerOnboarded && !isTrialLocal && !container) {
    return <ProviderPicker isOrg={isOrg} orgName={organization?.name} />;
  }

  // Terminal error — any status="error" that isn't `error_retrying` (see
  // isTerminalError above). Keyed off substatus, NOT `phase` (which resolves to
  // "container" for an error row). A retrying error deliberately does NOT match:
  // it falls through to the provisioning stepper below with a subtle "retrying"
  // note instead of this terminal card. Copy is honest about a real failure —
  // no "usually temporary" reassurance — since the container will not recover
  // on its own; Retry re-checks, but support is the real path if it persists.
  if (isTerminalError) {
    return (
      <div className="flex-1 flex items-center justify-center bg-[#faf7f2]">
        <div className="text-center space-y-6 max-w-sm">
          <StepperDisplay currentPhase={phase} steps={STEPS_PAID} error />
          <div className="space-y-2">
            <XCircle className="h-8 w-8 text-[#dc2626] mx-auto" />
            <h2 className="text-lg font-medium text-[#1a1a1a]">Setup failed</h2>
            <p className="text-sm text-[#8a8578]">
              {isOrg
                ? `We couldn't finish setting up the container for ${organization.name}. Retry below — if it keeps failing, contact support and we'll get it sorted.`
                : "We couldn't finish setting up your container. Retry below — if it keeps failing, contact support and we'll get it sorted."}
            </p>
          </div>
          <div className="flex gap-3 justify-center">
            <Button variant="outline" className="rounded-full border-[#e0dbd0]" onClick={() => refresh()}>
              Retry
            </Button>
            <Button variant="ghost" className="rounded-full" asChild>
              <a
                href="mailto:support@isol8.co"
                onClick={() => capture("support_contact_clicked", { source: "onboarding_stepper", phase })}
              >Contact Support</a>
            </Button>
          </div>
        </div>
      </div>
    );
  }

  // Provisioning stepper — animated 4-step flow
  const steps = STEPS_PAID;
  const currentIdx = steps.findIndex((s) => s.phase === phase);
  const currentStep = steps[currentIdx] || steps[0];

  return (
    <>
      <style>{`
        .provision-wrapper {
          flex: 1;
          display: flex;
          align-items: center;
          justify-content: center;
          background: #faf7f2;
        }
        .provision-card {
          text-align: center;
          max-width: 400px;
          width: 100%;
          padding: 48px 32px;
        }
        .provision-logo {
          margin-bottom: 32px;
        }
        .provision-stage {
          width: 80px;
          height: 80px;
          margin: 0 auto 32px;
          position: relative;
        }
        .provision-anim {
          position: absolute;
          inset: 0;
          display: flex;
          align-items: center;
          justify-content: center;
          opacity: 0;
          transition: opacity 400ms ease-out;
        }
        .provision-anim.active { opacity: 1; }

        /* Key spinner */
        .anim-key-circle {
          width: 56px; height: 56px;
          border: 2px solid #e0dbd0;
          border-top-color: #06402B;
          border-radius: 50%;
          animation: key-spin 1.2s linear infinite;
        }
        .anim-key-icon {
          position: absolute;
          inset: 0;
          display: flex;
          align-items: center;
          justify-content: center;
        }
        @keyframes key-spin { to { transform: rotate(360deg); } }

        /* Pod opening */
        .anim-pod { position: relative; width: 56px; height: 56px; }
        .pod-body {
          position: absolute; bottom: 0; left: 4px; right: 4px; height: 32px;
          background: #06402B; border-radius: 0 0 8px 8px;
        }
        .pod-window {
          width: 20px; height: 10px; background: #a8e6c6; border-radius: 4px;
          margin: 8px auto 0;
          /* Default flat glow; pulse only switches on once the lid opens, so
             the window doesn't visibly breathe during the brief pod-closed
             provisioning phase. */
          box-shadow: 0 0 0 0 rgba(168, 230, 198, 0);
        }
        .pod-open .pod-window {
          /* Slow ambient pulse — visible during the 3+ min stretch where
             the user is in gateway / channels-sidecars / agent sub-phases.
             The steam already loops above; this gives the green glow a
             gentle heartbeat so the eye registers continuous motion even
             after the open animation has settled. */
          animation: pod-window-pulse 2.6s ease-in-out infinite;
        }
        @keyframes pod-window-pulse {
          0%, 100% {
            box-shadow: 0 0 0 0 rgba(168, 230, 198, 0);
            background: #a8e6c6;
          }
          50% {
            box-shadow: 0 0 14px 2px rgba(168, 230, 198, 0.55);
            background: #c8f0d6;
          }
        }
        .pod-lid-l, .pod-lid-r {
          position: absolute; top: 16px; width: 50%; height: 12px;
          background: #06402B; transition: transform 600ms ease-out;
        }
        .pod-lid-l { left: 4px; border-radius: 8px 0 0 0; transform-origin: left center; }
        .pod-lid-r { right: 4px; border-radius: 0 8px 0 0; transform-origin: right center; }
        .pod-open .pod-lid-l { transform: rotate(-35deg); }
        .pod-open .pod-lid-r { transform: rotate(35deg); }
        .pod-steam-line {
          position: absolute; width: 2px; background: #c8e6c9; border-radius: 1px;
          opacity: 0; transition: opacity 400ms ease-out 300ms;
        }
        .pod-open .pod-steam-line { opacity: 1; animation: steam 1.5s ease-out infinite; }
        .pod-steam-line:nth-child(1) { left: 14px; height: 16px; top: -4px; animation-delay: 0s; }
        .pod-steam-line:nth-child(2) { left: 26px; height: 12px; top: -2px; animation-delay: 0.3s; }
        .pod-steam-line:nth-child(3) { left: 38px; height: 14px; top: -6px; animation-delay: 0.6s; }
        @keyframes steam {
          0% { transform: translateY(0); opacity: 0.7; }
          100% { transform: translateY(-18px); opacity: 0; }
        }

        /* Done check */
        .anim-done { display: flex; align-items: center; justify-content: center; }
        .check-path {
          stroke-dasharray: 30; stroke-dashoffset: 30;
          animation: draw-check 600ms ease-out forwards;
        }
        @keyframes draw-check { to { stroke-dashoffset: 0; } }

        /* Title fade */
        .provision-title {
          font-family: var(--font-lora-serif), serif;
          font-size: 22px; font-weight: 400; color: #1a1a1a;
          margin-bottom: 8px; transition: opacity 150ms ease-out;
        }
        .provision-desc {
          font-family: var(--font-dm-sans), sans-serif;
          font-size: 14px; color: #8a8578;
          margin-bottom: 32px; transition: opacity 150ms ease-out;
        }
        .provision-fading { opacity: 0; }

        /* Rotating idea suggestions — stacked eyebrow + phrase, space reserved
           so the layout never shifts during crossfade. */
        .provision-desc-ideas {
          display: flex; flex-direction: column; align-items: center;
          gap: 6px; min-height: 56px;
        }
        .provision-idea-eyebrow {
          font-family: var(--font-dm-sans), sans-serif;
          font-size: 12px; font-weight: 500;
          color: #a8a396; letter-spacing: 0.3px;
          text-transform: uppercase;
        }
        .provision-idea {
          font-family: var(--font-lora-serif), serif;
          font-size: 15px; font-style: italic;
          color: #4a4638; line-height: 1.4;
          max-width: 42ch; text-align: center;
          transition: opacity 180ms ease-out, transform 180ms ease-out;
          will-change: opacity, transform;
        }
        .provision-idea-fading { opacity: 0; transform: translateY(4px); }

        /* Progress dots */
        .provision-dots {
          display: flex; align-items: center; justify-content: center; gap: 0;
        }
        .provision-dot {
          width: 10px; height: 10px; border-radius: 50%;
          background: #e0dbd0; transition: background 300ms ease-out, transform 300ms ease-out;
        }
        .provision-dot.active { background: #06402B; transform: scale(1.2); }
        .provision-dot.done { background: #2d8a4e; }
        .provision-connector {
          width: 32px; height: 2px; background: #e0dbd0;
          transition: background 300ms ease-out;
        }
        .provision-connector.done { background: #2d8a4e; }

        .provision-free-banner {
          margin-top: 24px;
          padding: 12px 16px;
          background: #e8f5e9;
          border: 1px solid #c8e6c9;
          border-radius: 12px;
          font-size: 13px;
          color: #2d7a50;
        }
        .provision-free-sub {
          font-size: 12px; color: #8a8578; margin-top: 8px;
        }
        /* Live elapsed counter — sits below the dots so the user always
           sees the clock moving even before the soft-timeout copy appears. */
        .provision-elapsed {
          margin-top: 18px;
          font-family: var(--font-dm-sans), sans-serif;
          font-size: 12px;
          color: #a8a396;
          letter-spacing: 0.2px;
          font-variant-numeric: tabular-nums;
        }
        /* Soft-timeout block — calm, not alarming. The provision is still
           progressing; we're just inviting the user to re-check. */
        .provision-timeout {
          margin-top: 28px;
          padding-top: 20px;
          border-top: 1px solid #ece6d9;
          display: flex; flex-direction: column;
          align-items: center; gap: 14px;
          max-width: 360px;
          margin-left: auto; margin-right: auto;
        }
        .provision-timeout-msg {
          font-family: var(--font-dm-sans), sans-serif;
          font-size: 13px;
          color: #6e695d;
          line-height: 1.5;
          margin: 0;
          text-align: center;
        }
        .provision-timeout-actions {
          display: flex; gap: 8px; align-items: center;
        }

        @media (prefers-reduced-motion: reduce) {
          .anim-key-circle { animation: none; }
          .pod-steam-line { animation: none; }
          .pod-open .pod-window { animation: none; }
          .check-path { animation: none; stroke-dashoffset: 0; }
          .provision-title, .provision-desc { transition: none; }
        }
      `}</style>
      <div className="provision-wrapper">
        <div className="provision-card">
          <div className="provision-logo">
            <Logo size="lg" href={null} />
          </div>

          {/* Animation stage */}
          <div className="provision-stage" aria-hidden="true">
            {/* Step: payment/container — key spinner */}
            <div className={`provision-anim ${phase === "payment" || phase === "container" ? "active" : ""}`}>
              <div className="anim-key-circle" />
              <div className="anim-key-icon">
                <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="#06402B" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M21 2l-2 2m-7.61 7.61a5.5 5.5 0 1 1-7.78 7.78 5.5 5.5 0 0 1 7.78-7.78zm0 0L15.5 7.5m0 0l3 3L22 7l-3-3m-3.5 3.5L19 4" />
                </svg>
              </div>
            </div>

            {/* Step: gateway / channels-sidecars / agent — all reuse the
                pod-opening animation (the long cold-start window) so users
                see continuous motion as the stepper text advances through
                "Booting gateway..." -> "Starting channels..." -> "Starting
                agent...". One animation, three labels. */}
            <div
              className={`provision-anim ${
                phase === "gateway" ||
                phase === "channels-sidecars" ||
                phase === "agent"
                  ? "active"
                  : ""
              }`}
            >
              <div
                className={`anim-pod ${
                  phase === "gateway" ||
                  phase === "channels-sidecars" ||
                  phase === "agent"
                    ? "pod-open"
                    : ""
                }`}
              >
                <div className="pod-steam-line" /><div className="pod-steam-line" /><div className="pod-steam-line" />
                <div className="pod-lid-l" /><div className="pod-lid-r" />
                <div className="pod-body"><div className="pod-window" /></div>
              </div>
            </div>

            {/* Step: ready — checkmark */}
            <div className={`provision-anim ${(phase as string) === "ready" ? "active" : ""}`}>
              <div className="anim-done">
                <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="#06402B" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                  <polyline points="20 6 9 17 4 12" className="check-path" />
                </svg>
              </div>
            </div>
          </div>

          <h2
            className="provision-title"
            role="status"
            aria-live="polite"
            aria-atomic="true"
          >
            {currentStep?.activeLabel || "Setting up..."}
          </h2>
          <div className="provision-desc provision-desc-ideas">
            <RotatingIdeas
              eyebrow={
                isOrg
                  ? `Preparing workspace for ${organization?.name} — try asking your agent to…`
                  : "While you wait, try asking your agent to…"
              }
            />
          </div>

          {/* Progress dots */}
          <div className="provision-dots">
            {steps.map((step, i) => (
              <span key={step.phase}>
                {i > 0 && <span className={`provision-connector ${i <= currentIdx ? "done" : ""}`} />}
                <span className={`provision-dot ${i === currentIdx ? "active" : i < currentIdx ? "done" : ""}`} />
              </span>
            ))}
          </div>

          {/* Quiet elapsed counter — present from the start so the user can
              see the clock advancing, not a wall of unchanging spinner. */}
          {(phase === "container" ||
            phase === "gateway" ||
            phase === "channels-sidecars" ||
            phase === "agent") &&
            elapsedMs > 0 && (
              <div className="provision-elapsed" aria-live="polite">
                {formatElapsed(elapsedMs)} · cold starts can take a few minutes
              </div>
            )}

          {/* Transient error (status="error" + substatus="error_retrying"): the
              backend is auto-retrying, so we keep the stepper and add a calm,
              non-alarming line rather than the terminal "Setup failed" card. */}
          {isRetryingError && (
            <div className="provision-elapsed" role="status" aria-live="polite">
              Hit a snag — retrying automatically.
            </div>
          )}

          {timedOut && (
            <div className="provision-timeout">
              <p className="provision-timeout-msg">
                Still going. Re-check below — your progress is preserved.
              </p>
              <div className="provision-timeout-actions">
                <Button
                  variant="outline"
                  size="sm"
                  className="rounded-full border-[#e0dbd0]"
                  onClick={() => {
                    refresh();
                    setTimedOut(false);
                    provisioningStartRef.current = Date.now();
                    setElapsedMs(0);
                  }}
                >
                  Check again
                </Button>
                <Button variant="ghost" size="sm" asChild>
                  <a
                    href="mailto:support@isol8.co"
                    onClick={() => capture("support_contact_clicked", { source: "onboarding_stepper", phase })}
                  >Contact support</a>
                </Button>
              </div>
            </div>
          )}
        </div>
      </div>
    </>
  );
}


/**
 * Non-blocking "Teams needs attention" affordance layered over the ready app
 * when /container/status reports `teams_status: "failed"` — Teams (Friday/Atlas)
 * seeding failed but the container is healthy and chat works. Fixed, small, and
 * dismissible so it never blocks the app. Owns its own dismiss state so the
 * ready branch stays a plain `children` return with no extra parent hook.
 */
function TeamsDegradedNotice() {
  const [dismissed, setDismissed] = useState(false);
  useEffect(() => {
    capture("teams_degraded_notice_shown");
  }, []);
  if (dismissed) return null;
  return (
    <div
      role="status"
      aria-live="polite"
      className="fixed bottom-4 right-4 z-50 max-w-xs rounded-xl border border-[#e7c9a8] bg-[#fdf6ec] px-4 py-3 shadow-lg"
    >
      <div className="flex items-start gap-3">
        <div className="flex-1">
          <p className="text-sm font-medium text-[#7a4a12]">Teams needs attention</p>
          <p className="mt-1 text-xs text-[#8a6a3a]">
            Your team workspace didn&apos;t finish setting up, so some team
            features may be unavailable. Chat works normally.
          </p>
          <a
            href="mailto:support@isol8.co"
            onClick={() =>
              capture("support_contact_clicked", { source: "teams_degraded_notice" })
            }
            className="mt-2 inline-block text-xs font-medium text-[#06402B] underline"
          >
            Contact support
          </a>
        </div>
        <button
          type="button"
          aria-label="Dismiss"
          onClick={() => setDismissed(true)}
          className="text-lg leading-none text-[#b08a5a] hover:text-[#7a4a12]"
        >
          ×
        </button>
      </div>
    </div>
  );
}


function StepperDisplay({
  currentPhase,
  steps,
  error = false,
}: {
  currentPhase: Phase;
  steps: { phase: Phase; label: string; activeLabel: string }[];
  error?: boolean;
}) {
  const currentIdx = steps.findIndex((s) => s.phase === currentPhase);

  return (
    <div className="space-y-3 text-left mx-auto w-fit">
      {steps.map((step, idx) => {
        const isComplete = idx < currentIdx;
        const isCurrent = idx === currentIdx;
        const isErrorStep = error && isCurrent;

        return (
          <div key={step.phase} className="flex items-center gap-3">
            {isErrorStep ? (
              <XCircle className="h-5 w-5 text-[#dc2626] flex-shrink-0" />
            ) : isComplete ? (
              <CheckCircle className="h-5 w-5 text-[#2d8a4e] flex-shrink-0" />
            ) : isCurrent ? (
              <Loader2 className="h-5 w-5 animate-spin text-[#06402B] flex-shrink-0" />
            ) : (
              <Circle className="h-5 w-5 text-[#d4cfc5] flex-shrink-0" />
            )}
            <span
              className={
                isErrorStep
                  ? "text-sm text-[#dc2626]"
                  : isComplete
                    ? "text-sm text-[#1a1a1a]"
                    : isCurrent
                      ? "text-sm text-[#1a1a1a] font-medium"
                      : "text-sm text-[#c5bfb6]"
              }
            >
              {isCurrent && !error ? step.activeLabel : step.label}
            </span>
          </div>
        );
      })}
    </div>
  );
}

export function ProviderPicker({ isOrg, orgName }: { isOrg: boolean; orgName?: string }) {
  const api = useApi();
  const [loading, setLoading] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const handlePick = useCallback(async (id: ProviderChoiceId) => {
    setLoading(id);
    setError(null);
    capture("subscription_checkout_started", {
      provider_choice: id,
      owner_type: isOrg ? "org" : "personal",
      source: "provider_picker",
    });
    try {
      const resp = (await api.post("/billing/trial-checkout", {
        provider_choice: id,
      })) as { checkout_url?: string };
      if (resp?.checkout_url) {
        // Redirect to Stripe Checkout. After Checkout returns success, the
        // wizard re-mounts on /chat?checkout=success&provider=<id>; the
        // subscription.updated webhook persists provider_choice to the user
        // row, the wizard sees subscription_status === "trialing", and
        // advances into the provider phase. Without this round-trip the
        // user gets stuck in the payment phase forever.
        window.location.href = resp.checkout_url;
      } else {
        throw new Error("No checkout_url returned");
      }
    } catch (err) {
      capture("subscription_checkout_failed", {
        provider_choice: id,
        owner_type: isOrg ? "org" : "personal",
        source: "provider_picker",
        error: err instanceof Error ? err.message : "unknown",
      });
      setLoading(null);
      setError(err instanceof Error ? err.message : "Couldn't start checkout. Please try again.");
    }
  }, [api, isOrg]);

  return (
    <div className="flex-1 flex items-center justify-center p-8 bg-[#faf7f2]">
      <div className="max-w-5xl w-full space-y-8 text-center">
        <div className="space-y-3">
          <svg width="48" height="48" viewBox="0 0 100 100" fill="none" xmlns="http://www.w3.org/2000/svg" className="mx-auto mb-4">
            <rect width="100" height="100" rx="22" fill="#06402B" />
            <text x="50" y="68" textAnchor="middle" fontFamily="var(--font-lora-serif), serif" fontStyle="italic" fontSize="52" fill="white">8</text>
          </svg>
          <h2 className="text-2xl font-semibold tracking-tight text-[#1a1a1a] font-lora">
            One price. Three ways to power it.
          </h2>
          <p className="text-[#8a8578] text-sm max-w-md mx-auto">
            {isOrg
              ? `Pick how ${orgName} wants to pay for inference. The $50/month covers the always-on agent infrastructure.`
              : "Pick how you want to pay for inference. The $50/month covers your always-on agent infrastructure."}
          </p>
        </div>

        {error && (
          <div className="text-sm text-red-600 -mt-4">{error}</div>
        )}

        <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
          {PROVIDER_CARDS.map((card) => (
            <div
              key={card.id}
              className={
                "rounded-xl border p-6 space-y-4 bg-white text-left flex flex-col " +
                (card.highlighted ? "border-2 border-[#06402B]" : "border-[#e0dbd0]")
              }
            >
              <div className="space-y-1">
                <h3 className="font-medium text-[#1a1a1a]">{card.title}</h3>
                <p className="text-sm text-[#8a8578]">{card.subtitle}</p>
                <p className="text-xs text-[#06402B] font-medium">{card.trialNote}</p>
                {isOrg && card.id === "chatgpt_oauth" && (
                  <p className="text-xs text-[#8a8578]">
                    You connect your ChatGPT account (Pro recommended) — one
                    account powers the whole workspace.
                  </p>
                )}
              </div>
              <ul className="text-sm text-[#5a5549] space-y-2 flex-1">
                {card.bullets.map((b) => (
                  <li key={b} className="flex items-start gap-2">
                    <span aria-hidden className="text-[#06402B]">✓</span>
                    <span>{b}</span>
                  </li>
                ))}
              </ul>
              <Button
                className={
                  card.highlighted
                    ? "w-full rounded-full bg-[#06402B] hover:bg-[#0a5c3e] text-white"
                    : "w-full rounded-full border-[#e0dbd0] text-[#1a1a1a] hover:bg-[#f3efe6]"
                }
                variant={card.highlighted ? "default" : "outline"}
                onClick={() => handlePick(card.id)}
                disabled={loading !== null}
              >
                {loading === card.id ? <Loader2 className="h-4 w-4 animate-spin" /> : card.cta}
              </Button>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
