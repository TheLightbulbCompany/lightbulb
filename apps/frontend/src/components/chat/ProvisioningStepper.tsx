"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useSWRConfig } from "swr";
import { notifyApiError } from "@/lib/notify";
import { useSearchParams } from "next/navigation";

import { capture } from "@/lib/analytics";
import { setSignupStep } from "@/lib/signupFlow";
import { Loader2 } from "lucide-react";
import { useOrganization, useAuth } from "@clerk/nextjs";
import { Button } from "@/components/ui/button";
import { useBilling } from "@/hooks/useBilling";
import { useSuperAgent } from "@/hooks/useLiveEvents";
import { useProvisioningState, type BlockedPayload } from "@/hooks/useProvisioningState";
import { useRuntimeRpc } from "@/hooks/useRuntimeRpc";
import { useSystemHealth } from "@/hooks/useSystemHealth";
import { nextOnboardingCompletion } from "@/components/chat/onboardingAnalytics";
import { replaceSearchParams } from "@/lib/shellNav";
import { SUPPORT_MAILTO } from "@/lib/domain";
import { ChatGPTOAuthStep } from "@/components/chat/ChatGPTOAuthStep";
import { ByoKeyStep } from "@/components/chat/ByoKeyStep";
import { TeamBillingCard } from "@/components/chat/TeamBillingCard";
import { Logo, LogoMark } from "@/components/Logo";
import { providerComparisonCards } from "@/lib/providerCards";
// Provisioning copy + timing live in @lightbulb/shared so the web and mobile
// steppers stay in lockstep. Aliased to this file's historical local names so
// every usage site below is unchanged.
import {
  PROVISION_IDEAS,
  PROVISION_IDEA_ROTATION_MS as IDEA_ROTATION_MS,
  PROVISION_TIMEOUT_MS as TIMEOUT_MS,
  PROVISION_STUCK_MS as STUCK_MS,
  formatProvisionElapsed as formatElapsed,
  deriveUIPhase,
  type UIPhase,
} from "@lightbulb/shared";

// Frontend stepper phases. The cold-start "container", "gateway",
// "channels-sidecars", and "agent" steps all map to the same backend
// container_status="running" / gateway pool not-yet-connected window —
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
import { readLastKnown, writeLastKnown } from "@/lib/lastKnown";

function isProviderChoice(v: string | null): v is ProviderChoice {
  return v === "chatgpt_oauth" || v === "byo_key";
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
    desc: "Securing your key and setting up your workspace.",
  },
  openrouter_managed: {
    title: "Setting up your credits",
    desc: "Getting your managed models ready and setting up your workspace.",
  },
  default: {
    title: "Finishing setup",
    desc: "Just a moment while we get things ready.",
  },
};

// The ONE place setup steps get their user-facing words, keyed by the step ids
// the provisioning state already exposes. Plain language only — no
// "container"/"gateway" — because this is what a brand-new user reads while
// they wait. Swapping the data source (a live stepper) keeps this map.
export const SETUP_STEP_LABELS = {
  payment: "Confirming your plan…",
  container: "Setting up your workspace…",
  gateway: "Starting your agents…",
  "channels-sidecars": "Introducing your team…",
  agent: "Almost ready…",
  ready: "Ready!",
} as const satisfies Partial<Record<Phase, string>>;

const STEPS_PAID: { phase: Phase; activeLabel: string }[] = (
  Object.keys(SETUP_STEP_LABELS) as (keyof typeof SETUP_STEP_LABELS)[]
).map((phase) => ({ phase, activeLabel: SETUP_STEP_LABELS[phase] }));

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

// The stepper's short-lived loading holds replace the entire chat surface with
// an unlabelled centred spinner. On dev one of them sat there for 35s+ while
// /container/status reported `lifecycle: "provisioning"`, so the owner saw a
// bare spinner where the structured provisioning UI was supposed to be, and no
// text anywhere said what was being waited on. One label, one live region, one
// place to change it. (Motion is unchanged — same Loader2, same spin.)
/** How long a last-known-ready workspace may draw the app before the first status answer (W12-18). */
const HOLD_SKIP_MS = 3_000;

function StepperHold() {
  return (
    <div
      className="flex-1 flex items-center justify-center gap-2 text-sm text-[var(--midnight-muted)]"
      role="status"
      aria-live="polite"
    >
      <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
      <span>Getting your workspace ready…</span>
    </div>
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
  const superAgent = useSuperAgent();
  // Per-OWNER activation-marker key so a shared/QA browser doesn't let the first
  // account's activation suppress onboarding_ready for a later account's real
  // first cold start. The event is a person-level denominator (PostHog is
  // identified by Clerk user), so scope the once-ever marker to the owner
  // (org id for org workspaces, else the Clerk user id).
  const readyStorageKey = `${ONBOARDING_READY_STORAGE_KEY}:${organization?.id ?? clerkUserId ?? "anon"}`;
  const {
    account,
    isLoading: billingLoading,
    error: billingError,
    isSubscribed,
    refresh: refreshBilling,
    startCheckout,
  } = useBilling();
  // A workspace trial_local account is not paid and has no provider choice.
  // Keep it out of the pre-entitlement picker while its workspace is already
  // provisioning; provider setup happens only after workspace billing.
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
  // (/sign-up?provider=chatgpt_oauth|byo_key) and preserved
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

  // The single canonical `/container/status` reader — a pure consumer of the
  // ONE shared `useOnboardingStatus` read (spec PR-9, CLIENT-HOOK). There is no
  // poll: it advances on the live status events (below) and refetches on
  // visible/reconnect, all inside `useOnboardingStatus` itself.
  const {
    phase: gatePhase,
    blocked,
    container,
    refresh,
    error: statusError,
  } = useProvisioningState();
  const checkAgain = async () => {
    try {
      await refresh();
    } catch (err) {
      notifyApiError(err, "Couldn't check your workspace status");
    }
  };
  // Same /container/recover call HealthIndicator's CONTAINER_DOWN action uses
  // (useSystemHealth) — nothing re-dispatches a failed provision on its own, so
  // the failed state's Retry button below drives it explicitly.
  const { recover, isRecovering: isRetryingProvision } = useSystemHealth();
  const [retryFailed, setRetryFailed] = useState(false);

  // Private runtime health/onboarding events invalidate this shared status key
  // in LiveEventsProvider, so the stepper observes readiness through HTTP.

  // The trial_local row is created by the organization.created webhook, not
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

  // Provisioning is driven by the backend's own events (the organization.created
  // webhook, billing events, the user's Retry). The stepper is purely passive —
  // it reads the ONE shared status (see useProvisioningState above) and renders
  // the appropriate phase. No explicit POST /container/provision from here.

  const containerReady =
    container?.container_status === "running" || container?.container_substatus === "gateway_healthy";

  // Honest /container/status error contract: container_status="error" is a
  // REAL failure — previously masked as "provisioning" so a dead container
  // spun a stepper forever. The UI keys off `container_substatus`, NEVER
  // `runtime_phase` (which still resolves to a cold-start value like
  // "provisioning" for an error row, so it could never tell a dead
  // container from a live one).
  //
  // Every error row is TERMINAL: the backend writes one only for a deterministic
  // failure or once the state machine's retries are exhausted (a transient failure
  // it is still retrying writes nothing). Keyed off `container_status`, never a
  // specific substatus, so a bare error or any future substatus still gets the
  // Retry/Support CTA. `last_error` carries the reason.
  const isTerminalError = container?.container_status === "error";

  // A 200 with no container row: the workspace's first provision never started (its
  // organization.created webhook gave up). Nothing re-drives it on its own, so the
  // banner's check starts it through /container/recover, exactly like a failed one.
  const needsRecover = isTerminalError || (container != null && container.container_status == null);

  // AgentsBanner's "Check again"/"Retry" button. A terminal error (or no row) needs
  // the real recover call (see the useSystemHealth destructure above); every
  // other alarming state (stuck/slow) is a plain re-read.
  const handleAgentsBannerCheck = useCallback(async () => {
    if (!needsRecover) {
      refresh();
      return;
    }
    setRetryFailed(false);
    const result = await recover();
    if (result === null) {
      setRetryFailed(true);
      return;
    }
    refresh();
  }, [needsRecover, recover, refresh]);

  // Readiness decision (backend runtime_phase="provisioning"/"starting"/
  // "ready", lifecycle="parked"/"error"/…) is sourced from the ONE shared
  // deriveUIPhase (packages/shared/src/hooks/statusContract.ts) instead of
  // being reconstructed here — the useMemo ladder this replaced was the
  // web-specific half of the byte-forked duplicate mobile's
  // containerStatus.ts carried (mobile onboarding-parity Slice 1/COMPOSER).
  // `/container/status`'s 200 body IS the composed StatusContract verbatim
  // (see routers/container.py::_status_response — no legacy flat
  // duplicates), so the SWR hook's `container` value already IS one.
  const derived = container ? deriveUIPhase(container) : null;

  // Poll /container/status while the CURRENT phase is pre-ready. Derived,
  // not latched: an earlier one-way `reachedReady` latch turned polling off
  // forever after the first ready — so a later regression (reprovision /
  // wipe / scale-to-zero) rendered the cold-start UI with polling
  // permanently dead (verified prod incident: frozen 15+ min over a ready
  // backend). Deriving from the live phase re-arms automatically on
  // regression and stops at ready/parked (the WS is the live channel
  // there). This only gates the gateway-health RPC below now — the shared
  // `useOnboardingStatus` bounded loop behind `useProvisioningState` polls
  // unconditionally on its own cadence regardless of this flag.
  const shouldPollContainer =
    (isSubscribed || isTrialLocal) && derived?.phase !== "ready" && derived?.phase !== "parked";

  // Poll gateway health every 3s once container looks ready
  const { data: gatewayHealth } = useRuntimeRpc<Record<string, unknown>>(
    shouldPollContainer && containerReady ? "health" : null,
    undefined,
    { refreshInterval: 3000, dedupingInterval: 2000 },
  );

  // Ready re-kick: reaching "ready" is observed on the HTTP status poll — a
  // channel with zero coupling to the gateway-WS RPC path. Any gateway SWR
  // key that errored through the cold start is dead by then: the fixed
  // 3s retry chain is capped (~5min), and SWR's refreshInterval never
  // fires while a key holds an error — so the sidebar roster, config.get
  // and health stay failed until a remount, which is why a stuck wake used
  // to need a full page reload. Revive every gateway RPC key on the
  // observed not-ready→ready transition so "ready" always means "data
  // loads now", even when the status_change(HEALTHY) push was lost. Edge-
  // triggered off the poll data (never a timer), and only on a LIVE
  // transition — a warm page load that first observes "ready" skips the
  // sweep (its keys are mount-revalidating already).
  const { mutate: reviveGatewayKeys } = useSWRConfig();
  const readyRekickPhaseRef = useRef<UIPhase | null>(null);
  useEffect(() => {
    const prev = readyRekickPhaseRef.current;
    const current = derived?.phase ?? null;
    if (current !== null) readyRekickPhaseRef.current = current;
    if (current !== "ready") return;
    if (prev === null || prev === "ready" || prev === "parked") return;
    void reviveGatewayKeys(
      (key) => typeof key === "string" && key.startsWith("rpc|"),
    );
  }, [derived?.phase, reviveGatewayKeys]);

  // Track previous backend phase via a ref so we can detect regression
  // edges *during* render. When the backend goes ready -> not-ready
  // (e.g., scale-to-zero brought it back to provisioning) the
  // elapsedMs state is still the stale value from the prior cold-start
  // session — the reset effect below clears it but only after this
  // render commits. Without this regression detection, the stepper
  // would flash "agent" / "timed out" for a single frame before
  // settling onto the correct early sub-phase.
  //
  // Sourced from `derived.phase` (the ONE shared deriveUIPhase output),
  // NOT the deleted legacy flat `container.phase` field — "parked" is a
  // `Lifecycle` refinement in the contract, not a `RuntimePhase` value, so
  // `derived.phase` (which folds both in) is the correct read here.
  const previousBackendPhaseRef = useRef<UIPhase | undefined>(undefined);
  const isRegressingFromReady =
    // eslint-disable-next-line react-hooks/refs
    (previousBackendPhaseRef.current === "ready" || previousBackendPhaseRef.current === "parked") &&
    derived?.phase !== undefined &&
    derived.phase !== "ready" &&
    // Parked is ready-equivalent: a healthy box the backend put to sleep,
    // one intent away from running. Treating ready->parked (or parked
    // holding steady) as a regression would swap the whole chat surface
    // for a cold-start stepper over a perfectly readable page.
    derived.phase !== "parked";
  // Mutate the ref during render — refs are React-blessed for this
  // pattern (derived state from prop transitions).
  // eslint-disable-next-line react-hooks/refs
  previousBackendPhaseRef.current = derived?.phase;

  // Derive phase purely from data. The cold-start window (backend
  // runtimePhase="starting", roughly ECS RUNNING through gateway-connected)
  // is split into three visible UI sub-phases by elapsed time so the
  // user sees concrete progress through what is currently a 5+min wait
  // in upstream's sidecars.channels. That elapsed-time sub-stepping is a
  // presentation-only refinement of ONE composer-decided phase
  // ("cold_start") — not itself a readiness decision — so it stays here
  // rather than in the shared deriveUIPhase.
  const phase: Phase = useMemo(() => {
    // Just regressed from ready -> not-ready? Force the conservative
    // early step until the reset effect clears stale timing state.
    if (isRegressingFromReady) return "container";
    // Provider-specific signup step runs before both payment fallback and
    // container provision. Checkout and OAuth redirects preserve ?provider=;
    // if billing/provider state is still catching up, showing the generic
    // payment card here strands the user and hides the real provider gate.
    if (needsProviderStep) return "provider";
    // Pre-signup user lands in payment phase to pick a provider path.
    // Recovery flow skips billing — the user already has a subscription. A
    // provisioning container (200 from /container/status) means the backend
    // already gated this workspace in — so don't force payment while the
    // container is coming up, even if billing hasn't caught up to the trial.
    if (trigger !== "recovery" && !isSubscribed && !isTrialLocal && !container) return "payment";

    if (!container || !derived) return "container";
    // Parked (wake-gated trial box, deliberately asleep): render the app —
    // content stays readable, and any interaction / message send wakes it.
    // Checked BEFORE the error/status branches: a parked row's status is
    // "stopped", which the legacy checks would misread as a cold start.
    if (derived.phase === "parked") return resolvePostAgentPhase();
    if (derived.phase === "error") return "container";
    if (derived.runtimePhase === "provisioning") {
      // substatus "task_running" = the ECS task is RUNNING with an IP but
      // the gateway port hasn't opened yet (image pulled, OpenClaw
      // booting) — the real "gateway" window.
      return container.container_substatus === "task_running" ? "gateway" : "container";
    }
    if (!containerReady && derived.runtimePhase !== "starting") return "container";

    if (derived.runtimePhase === "starting" || (!derived.chatAvailable && !gatewayHealth)) {
      // runtimePhase="starting" means the gateway port answered the TCP
      // probe: the gateway is up and OpenClaw is loading channels/sidecars.
      // Once the health RPC answers, only the pool handshake remains —
      // the "agent" step. Every transition here is a real signal.
      return gatewayHealth ? "agent" : "channels-sidecars";
    }

    // derived.runtimePhase === "ready" (chatAvailable) — chat is ready.
    // Channel setup is no longer forced here; it is nudged via the Get
    // Started guide and lives in Settings → Channels.
    return resolvePostAgentPhase();
  }, [trigger, isSubscribed, isTrialLocal, container, derived, containerReady, gatewayHealth, needsProviderStep, isRegressingFromReady]);

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
    if (container?.runtime_phase === "provisioning" || container?.runtime_phase === "starting") {
      sawColdStartRef.current = true;
    }
  }, [container?.runtime_phase]);
  // The step the analytics effect last REPORTED (not merely computed), so a
  // completion is only recorded for a step that was itself a genuine entry.
  const lastReportedStepRef = useRef<typeof phase | null>(null);
  useEffect(() => {
    // Until billing and the first /container/status answer are in, every phase
    // below is a guess: a returning user reads as "payment" for a frame because
    // neither isSubscribed nor the container row has loaded. Report nothing,
    // and leave prevPhaseRef untouched so the first settled phase is a real entry.
    if (billingLoading || gatePhase === "loading") return;
    const prev = prevPhaseRef.current;
    prevPhaseRef.current = phase;
    if (prev === phase) return;
    // Tell the app-root `signup_exit` listener which step the user is on.
    // Only while this is a GENUINE first-time flow: the "recovery" trigger is
    // an existing customer's container restarting, and even on the onboarding
    // trigger an already-provisioned user's /chat load renders phase="container"
    // for a frame before the data arrives. Neither is a signup, and an exit
    // during either must not be counted as a drop-off — so the same
    // witnessed-cold-start guard `onboarding_ready` uses applies here. The
    // pre-container phases (payment/provider) only render for owners who have
    // not provisioned yet, so they need no such witness.
    const inSignupFlow =
      trigger === "onboarding" &&
      phase !== "ready" &&
      (phase === "payment" || phase === "provider" || sawColdStartRef.current);
    setSignupStep(inSignupFlow ? phase : null);
    // Same guard for the step event: a returning user's /chat remount used to
    // report a phantom payment → container → ready traversal (billing and the
    // container row arrive a frame late, so the first computed phase is
    // "payment"), which read as a repeated onboarding on their timeline.
    if (inSignupFlow) {
      lastReportedStepRef.current = phase;
      capture("onboarding_step_entered", {
        step_name: phase,
        step_index: ONBOARDING_PHASE_ORDER.indexOf(phase),
        ...(phase === "provider" && providerChoice !== null
          ? { provider_choice: providerChoice }
          : {}),
      });
    }
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
    // A step completes only if its entry was reported above: a returning
    // user's settled ready state must not "complete" a payment step they never
    // genuinely entered.
    const completion = nextOnboardingCompletion(prev, phase, STEPS_PAID);
    if (completion && lastReportedStepRef.current === prev) {
      capture("onboarding_step_completed", { ...completion });
    }
  }, [phase, providerChoice, readyStorageKey, trigger, billingLoading, gatePhase]);

  // `trial_started` is emitted SERVER-SIDE ONLY, by the one write that
  // actually creates the trial_local row (core/services/user_provision.py).
  // It used to fire from here as well, guarded by an owner-scoped
  // localStorage marker — and the two emitters disagreed: 161 events over
  // 93 people in 90 days (106 server / 55 client), i.e. every trial that
  // started in a browser that kept its storage was counted twice, and
  // every trial started in one that did not (in-app webviews, private
  // mode) was counted once. A conversion denominator cannot be a
  // best-effort client guess about a row the server alone writes.

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

  // Elapsed-time tick ONLY — a display clock, never a status fetch. The status
  // advances on live events and visible/reconnect refetches (see
  // `useOnboardingStatus`); past TIMEOUT_MS the timed-out card offers a manual
  // check, which is the backstop for an event AppSync dropped.
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
    const tick = () => {
      const start = provisioningStartRef.current;
      if (start === null) return;
      const ms = Date.now() - start;
      setElapsedMs(ms);
      if (ms > TIMEOUT_MS) setTimedOut(true);
    };
    tick();
    const interval = setInterval(tick, 1000);
    return () => clearInterval(interval);
  }, [phase]);

  // W12-18: a workspace this browser last saw ready skips the hold below while /billing/account and the first
  // /container/status answer (1-3.5 s on a dev cold load): the app draws at once and this gate still decides when
  // they do. Any settled answer that is not ready forgets it, so the next load waits again. Never past an error (a
  // token that won't verify, a failed read), and never past HOLD_SKIP_MS: after that the hold shows as before.
  const passedScope = `${clerkUserId ?? "anon"}:${organization?.id ?? "personal"}`;
  const settled = !billingLoading && gatePhase !== "loading";
  useEffect(() => {
    if (phase === "ready") writeLastKnown("gatePassed", passedScope, true);
    else if (settled) writeLastKnown("gatePassed", passedScope, false);
  }, [phase, settled, passedScope]);
  const [skipExpired, setSkipExpired] = useState(false);
  useEffect(() => {
    if (settled) return;
    const timer = setTimeout(() => setSkipExpired(true), HOLD_SKIP_MS);
    return () => clearTimeout(timer);
  }, [settled]);
  const skipHold = !skipExpired && !statusError && !billingError && readLastKnown("gatePassed", passedScope) === true;

  // Chat-first onboarding begins after this provisioning gate. The backend
  // captures the first canonical-room human message only when normal per-room
  // dispatch selected an eligible agent; this component owns no brief form,
  // draft replay, or synthetic kickoff state.

  // Ready — render children. `teams_state: "failed"` is a NON-BLOCKING
  // signal on the composed contract: Teams (Friday/Atlas) seeding failed
  // but the container itself is healthy and chat works — so we render the
  // app as normal and layer a small dismissible "Teams needs attention"
  // notice over it, never a blocking interstitial.
  //
  if (phase === "ready") {
    const teamsDegraded = container?.teams_state === "failed";
    return (
      <>
        {children}
        {teamsDegraded && (
          <div className="fixed bottom-4 right-4 z-(--z-toast) flex flex-col items-end gap-2">
            {teamsDegraded && <TeamsDegradedNotice />}
          </div>
        )}
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
            background: var(--cream);
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
            border: 2px solid var(--edge);
            border-top-color: var(--midnight);
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
            font-family: var(--font-serif), serif;
            font-size: 22px; font-weight: 400; color: var(--midnight);
            margin-bottom: 8px;
          }
          .provision-desc {
            font-family: var(--font-sans), sans-serif;
            font-size: 14px; color: var(--midnight-muted);
            margin-bottom: 24px;
          }
          .provision-muted {
            font-family: var(--font-sans), sans-serif;
            font-size: 13px;
            color: var(--midnight-muted);
            margin: 0;
          }
          .provision-timeout {
            margin-top: 28px;
            padding-top: 20px;
            border-top: 1px solid var(--edge);
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
                  <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="var(--midnight)" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
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
                  className="rounded-full border-[var(--edge)]"
                  onClick={checkAgain}
                >
                  Check again
                </Button>
                <Button variant="ghost" size="sm" asChild>
                  <a
                    href={SUPPORT_MAILTO}
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
  // Render the ProviderPicker by intent here. Its workspace-admin CTA posts
  // the unified checkout endpoint; personal contexts are directed to select a
  // workspace. Placed before billing/onboarded fall-through so
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
  // truth. Checkout deliberately does not write it; POST /billing/provider
  // owns the post-entitlement selection (including ByoKeyStep's sub-provider).
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
      <div className="flex-1 flex items-center justify-center p-6 bg-[var(--cream)]">
        <div className="w-full max-w-md">
          {providerChoice === "chatgpt_oauth" && (
            <ChatGPTOAuthStep onComplete={handleProviderComplete} />
          )}
          {providerChoice === "byo_key" && (
            <ByoKeyStep onComplete={handleProviderComplete} />
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
      <div className="flex-1 flex items-center justify-center bg-[var(--cream)]">
        <div className="w-full max-w-[400px] px-8 py-12 text-center">
          <div className="mb-8 flex justify-center">
            <Logo size="lg" href={null} />
          </div>
          <div className="mx-auto mb-8 flex h-20 w-20 items-center justify-center">
            <Loader2 className="h-10 w-10 animate-spin text-[var(--midnight)]" aria-hidden="true" />
          </div>
          <h2
            role="status"
            aria-live="polite"
            aria-atomic="true"
            className="mb-2 text-title font-normal text-[var(--midnight)]"
            style={{ fontFamily: "var(--font-serif), serif" }}
          >
            {copy.title}
          </h2>
          <p
            className="text-sm text-[var(--midnight-muted)]"
            style={{ fontFamily: "var(--font-sans), sans-serif" }}
          >
            {copy.desc}
          </p>
        </div>
      </div>
    );
  }

  // Workspace billing paywall: the gate returns team_billing_required for an
  // owner (or member) with no billing row. The card is admin-gated
  // internally — a member sees "ask your workspace admin" with no CTA — and takes
  // the gate's own `action.admin_only` rather than assuming it.
  //
  // The wrapper mirrors the generic blocked card below (logo centerpiece +
  // Check again / Contact support footer on the cream canvas) so a blocked
  // org lands on the same shaped screen as every other gate; only the card in
  // the middle differs.
  if (gatePhase === "blocked" && blocked && blocked.code === "team_billing_required") {
    return (
      <div className="flex-1 flex items-center justify-center bg-[var(--cream)] px-6 py-12">
        <div className="flex w-full max-w-[440px] flex-col items-center">
          <div className="mb-8 flex justify-center">
            <Logo size="lg" href={null} />
          </div>
          <TeamBillingCard
            workspacePriceUsd={account?.workspace_price_usd}
            ownerRole={blocked.owner_role}
            adminOnly={blocked.action.admin_only}
            isSubscribed={false}
            onSubscribe={() => startCheckout("workspace")}
          />
          <div className="mt-6 flex items-center gap-2">
            <Button
              variant="outline"
              size="sm"
              className="rounded-full border-[var(--edge)]"
              onClick={checkAgain}
            >
              Check again
            </Button>
            <Button variant="ghost" size="sm" asChild>
              <a
                href={SUPPORT_MAILTO}
                onClick={() => capture("support_contact_clicked", { source: "onboarding_stepper", phase })}
              >Contact support</a>
            </Button>
          </div>
        </div>
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
            background: var(--cream);
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
            border: 2px solid var(--edge);
            border-top-color: var(--midnight);
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
            font-family: var(--font-serif), serif;
            font-size: 22px; font-weight: 400; color: var(--midnight);
            margin-bottom: 8px;
          }
          .provision-desc {
            font-family: var(--font-sans), sans-serif;
            font-size: 14px; color: var(--midnight-muted);
            margin-bottom: 24px;
          }
          .provision-cta {
            display: inline-block;
            padding: 10px 22px;
            border-radius: 9999px;
            background: var(--midnight);
            color: var(--cream);
            font-family: var(--font-sans), sans-serif;
            font-size: 14px;
            font-weight: 500;
            text-decoration: none;
          }
          .provision-cta:hover { background: var(--midnight-soft); }
          .provision-muted {
            font-family: var(--font-sans), sans-serif;
            font-size: 13px;
            color: var(--midnight-muted);
            margin: 0;
          }
          .provision-timeout {
            margin-top: 28px;
            padding-top: 20px;
            border-top: 1px solid var(--edge);
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
                  <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="var(--midnight)" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
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
                  className="rounded-full border-[var(--edge)]"
                  onClick={checkAgain}
                >
                  Check again
                </Button>
                <Button variant="ghost" size="sm" asChild>
                  <a
                    href={SUPPORT_MAILTO}
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
  // out means the next thing a new trial user sees is workspace provisioning,
  // not a payment step they don't need.
  if (billingLoading || gatePhase === "loading") {
    // The same shape as the ready branch above, so the app is not remounted when the answers land.
    return skipHold ? <>{children}{null}</> : <StepperHold />;
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
  //   3. A trial_local workspace: it is not paid and has no provider choice.
  //      The `isTrialLocal` guard keeps an already-provisioning workspace off
  //      the picker while the billing state catches up.
  //   4. A provisioning container (200 from /container/status) means the
  //      backend already gated this workspace in. /billing/account may not
  //      have caught up yet, so a live container bypasses the picker too.
  const ownerOnboarded = isSubscribed && Boolean(account?.provider_choice);
  if (!ownerOnboarded && !isTrialLocal && !container) {
    return <ProviderPicker isOrg={isOrg} orgName={organization?.name} />;
  }

  // Past the second tier we stop claiming progress. Derived, not state, so
  // "Check again" cannot erase it — the old handler reset `timedOut` AND
  // `elapsedMs` to zero, which is how 21 hours kept looking like 5 minutes.
  const stuck = elapsedMs > STUCK_MS;

  // Super-agent: #general opens at once and takes a message; the setup's real progress is the banner over it (the
  // first message waits, admitted, until the runtime answers). Never a card in front of the chat.
  if (superAgent) {
    return (
      // The app first, the banner after (drawn on top by `order-first`): every branch keeps children at the same
      // place, so a box that stops or starts never remounts the app (composer text and scroll survive).
      <>
        {children}
        <AgentsBanner state={isTerminalError ? "failed" : stuck ? "stuck" : timedOut ? "slow" : "welcome"} phase={phase}
          onCheck={handleAgentsBannerCheck}
          onSupport={() => capture("support_contact_clicked", { source: "agents_banner", phase, stuck })}
          isChecking={needsRecover && isRetryingProvision} checkFailed={needsRecover && retryFailed} />
      </>
    );
  }

  // Every workspace/plan/provider gate above has passed. Messaging never needs
  // the container (HTTP + DynamoDB + AppSync), so a starting, stuck or failed
  // one is a banner over the working app, not a card in front of it. Only the
  // window before the first /container/status answer keeps the card below.
  if (container) {
    return (
      <>
        {children}
        <AgentsBanner
          state={
            isTerminalError
              ? "failed"
              : stuck
                ? "stuck"
                : timedOut
                  ? "slow"
                  : container.container_status == null
                    ? "not_started"
                    : "starting"
          }
          phase={phase}
          onCheck={handleAgentsBannerCheck}
          onSupport={() => capture("support_contact_clicked", { source: "agents_banner", phase, stuck })}
          isChecking={needsRecover && isRetryingProvision}
          checkFailed={needsRecover && retryFailed}
        />
      </>
    );
  }

  // Provisioning stepper — animated 4-step flow
  const steps = STEPS_PAID;
  const currentIdx = steps.findIndex((s) => s.phase === phase);
  const currentStep = steps[currentIdx] || steps[0];
  // While the failure is known, the pod animation would keep steaming as if
  // work were still happening.
  const podAnimating =
    (phase === "gateway" || phase === "channels-sidecars" || phase === "agent");

  return (
    <>
      <style>{`
        .provision-wrapper {
          flex: 1;
          display: flex;
          align-items: center;
          justify-content: center;
          background: var(--cream);
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
          border: 2px solid var(--edge);
          border-top-color: var(--midnight);
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
          background: var(--midnight); border-radius: 0 0 8px 8px;
        }
        /* --ok-reversed, not --ok: the pod body is its own midnight ground,
           so the window is a state mark on midnight even on the cream card. */
        .pod-window {
          width: 20px; height: 10px; background: var(--ok-reversed); border-radius: 4px;
          margin: 8px auto 0;
          /* Default flat glow; pulse only switches on once the lid opens, so
             the window doesn't visibly breathe during the brief pod-closed
             provisioning phase. */
          box-shadow: 0 0 0 0 transparent;
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
            box-shadow: 0 0 0 0 transparent;
          }
          50% {
            box-shadow: 0 0 14px 2px color-mix(in srgb, var(--ok-reversed) 55%, transparent);
          }
        }
        .pod-lid-l, .pod-lid-r {
          position: absolute; top: 16px; width: 50%; height: 12px;
          background: var(--midnight); transition: transform 600ms ease-out;
        }
        .pod-lid-l { left: 4px; border-radius: 8px 0 0 0; transform-origin: left center; }
        .pod-lid-r { right: 4px; border-radius: 0 8px 0 0; transform-origin: right center; }
        .pod-open .pod-lid-l { transform: rotate(-35deg); }
        .pod-open .pod-lid-r { transform: rotate(35deg); }
        .pod-steam-line {
          position: absolute; width: 2px; background: var(--midnight-faint); border-radius: 1px;
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
          font-family: var(--font-serif), serif;
          font-size: 22px; font-weight: 400; color: var(--midnight);
          margin-bottom: 8px; transition: opacity 150ms ease-out;
        }
        .provision-desc {
          font-family: var(--font-sans), sans-serif;
          font-size: 14px; color: var(--midnight-muted);
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
          font-family: var(--font-sans), sans-serif;
          font-size: 12px; font-weight: 500;
          color: var(--midnight-label); letter-spacing: 0.3px;
          text-transform: uppercase;
        }
        .provision-idea {
          font-family: var(--font-serif), serif;
          font-size: 15px; font-style: italic;
          color: var(--midnight-soft); line-height: 1.4;
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
          background: var(--edge); transition: background 300ms ease-out, transform 300ms ease-out;
        }
        .provision-dot.active { background: var(--midnight); transform: scale(1.2); }
        .provision-dot.done { background: var(--ok); }
        .provision-connector {
          width: 32px; height: 2px; background: var(--edge);
          transition: background 300ms ease-out;
        }
        .provision-connector.done { background: var(--ok); }

        .provision-free-banner {
          margin-top: 24px;
          padding: 12px 16px;
          background: var(--cream);
          border: 1px solid var(--ok);
          border-radius: 12px;
          font-size: 13px;
          color: var(--ok);
        }
        .provision-free-sub {
          font-size: 12px; color: var(--midnight-muted); margin-top: 8px;
        }
        /* Live elapsed counter — sits below the dots so the user always
           sees the clock moving even before the soft-timeout copy appears. */
        .provision-elapsed {
          margin-top: 18px;
          font-family: var(--font-sans), sans-serif;
          font-size: 12px;
          color: var(--midnight-muted);
          letter-spacing: 0.2px;
          font-variant-numeric: tabular-nums;
        }
        /* Soft-timeout block — calm, not alarming. The provision is still
           progressing; we're just inviting the user to re-check. */
        .provision-timeout {
          margin-top: 28px;
          padding-top: 20px;
          border-top: 1px solid var(--edge);
          display: flex; flex-direction: column;
          align-items: center; gap: 14px;
          max-width: 360px;
          margin-left: auto; margin-right: auto;
        }
        .provision-timeout-msg {
          font-family: var(--font-sans), sans-serif;
          font-size: 13px;
          color: var(--midnight-muted);
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
            <div
              className={`provision-anim ${
                phase === "payment" || phase === "container" ? "active" : ""
              }`}
            >
              <div className="anim-key-circle" />
              <div className="anim-key-icon">
                <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="var(--midnight)" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M21 2l-2 2m-7.61 7.61a5.5 5.5 0 1 1-7.78 7.78 5.5 5.5 0 0 1 7.78-7.78zm0 0L15.5 7.5m0 0l3 3L22 7l-3-3m-3.5 3.5L19 4" />
                </svg>
              </div>
            </div>

            {/* Step: gateway / channels-sidecars / agent — all reuse the
                pod-opening animation (the long cold-start window) so users
                see continuous motion as the stepper text advances through
                "Booting gateway..." -> "Starting channels..." -> "Starting
                agent...". One animation, three labels. */}
            <div className={`provision-anim ${podAnimating ? "active" : ""}`}>
              <div className={`anim-pod ${podAnimating ? "pod-open" : ""}`}>
                <div className="pod-steam-line" /><div className="pod-steam-line" /><div className="pod-steam-line" />
                <div className="pod-lid-l" /><div className="pod-lid-r" />
                <div className="pod-body"><div className="pod-window" /></div>
              </div>
            </div>

            {/* Step: ready — checkmark */}
            <div className={`provision-anim ${(phase as string) === "ready" ? "active" : ""}`}>
              <div className="anim-done">
                <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="var(--midnight)" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
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
          {/* Rotating "while you wait, try asking your agent to…" prompts are
              an invitation to keep waiting. Once the failure is known there is
              nothing to wait for, so they go. */}
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
              see the clock advancing, not a wall of unchanging spinner. The
              "cold starts can take a few minutes" caption is a claim about the
              future, so it only survives while that is still plausibly true:
              not once the failure is known, and not past the stuck tier. */}
          {(phase === "container" ||
              phase === "gateway" ||
              phase === "channels-sidecars" ||
              phase === "agent") &&
            elapsedMs > 0 && (
              /* aria-live="off" is deliberate: this value ticks once per
                 second and provisioning routinely runs minutes, so a live
                 region here reads the elapsed time to a screen reader once per
                 second for the whole of onboarding and buries the step
                 transitions announced from the same region by the
                 `provision-title` heading above. This is the number, not the
                 news. */
              <div className="provision-elapsed" aria-live="off">
                {formatElapsed(elapsedMs)}
                {!stuck && " · cold starts can take a few minutes"}
              </div>
            )}

          {/* The known-failure card. Inline, in the flow of this card — the
              ready branch's `fixed` corner stack is the only one, and there is
              no app behind this screen to layer a toast over. */}
          {timedOut && (
            <div className="provision-timeout">
              {/* Two tiers. The first is honest for a slow-but-live cold start.
                  The second stops claiming it is "still going" — past 15 min
                  nothing here is normal, and saying so is the only useful
                  thing this screen can do. */}
              <p className="provision-timeout-msg">
                {stuck
                  ? "This is taking longer than it should — something is stuck on our side. Setup normally finishes in a few minutes. Your progress is saved; contact us and we'll get it moving."
                  : "Still going. Re-check below — your progress is preserved."}
              </p>
              <div className="provision-timeout-actions">
                <Button
                  variant="outline"
                  size="sm"
                  className="rounded-full border-[var(--edge)]"
                  // Re-poll ONLY. This used to also clear `timedOut` and reset
                  // the clock to 0:00, so the one button an escalated owner
                  // presses erased their own escalation and started the 5-min
                  // budget over — 21 hours of stall read as five minutes,
                  // repeatedly.
                  onClick={checkAgain}
                >
                  Check again
                </Button>
                <Button variant="ghost" size="sm" asChild>
                  <a
                    href={SUPPORT_MAILTO}
                    onClick={() => capture("support_contact_clicked", { source: "onboarding_stepper", phase, stuck })}
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


const AGENTS_BANNER_COPY = {
  starting: "Your team is getting ready — it'll be ready in about a minute.",
  // No container row: nothing is provisioning (the org.created webhook is the only automatic
  // trigger since the status poll retired). Offer "Check again" at once rather than after the
  // 5-minute slow threshold; it starts the workspace through /container/recover.
  not_started: "Your team hasn't started setting up yet.",
  slow: "Still going — your agents are taking a little longer than usual. You can keep messaging in the meantime.",
  stuck: "Starting your agents is taking longer than it should — something is stuck on our side. Messaging still works; contact us and we'll get it moving.",
  failed: "Setup failed — your agents couldn't start. Messaging still works; retry, or contact support if it keeps failing.",
  // Super-agent, first minutes: the chat is open and takes a message now; Lightbulb answers it once setup finishes.
  welcome: "You can write to Lightbulb now — it answers as soon as setup finishes.",
} as const;

/**
 * Where the banner's phase sits on the SAME six-step ladder the setup card was
 * drawing a moment ago (`STEPS_PAID`), so handing over from card to banner
 * keeps one numbering rather than inventing a second.
 *
 * Returns null for any phase that isn't on that ladder. That null is the whole
 * point of this function: the banner then falls back to its flat copy. A
 * position we can't resolve from the real phase is never guessed, and nothing
 * here is derived from elapsed time — the card's own clock is the only thing
 * allowed to move on its own.
 */
export function agentsBannerProgress(
  phase: Phase,
): { step: number; total: number; activeLabel: string } | null {
  const idx = STEPS_PAID.findIndex((s) => s.phase === phase);
  if (idx < 0) return null;
  return { step: idx + 1, total: STEPS_PAID.length, activeLabel: STEPS_PAID[idx].activeLabel };
}

// The only states allowed to draw the bar, each with the sentence it uses once
// the step label is carrying the "what's happening" half. `starting` drops its
// now-redundant "Your agents are getting ready." — the label above it already
// says which step is running — and keeps only the reassurance; `slow` keeps its
// full sentence, because it says something the step label does not.
//
// `stuck` and `failed` are absent deliberately: a bar sitting at 4 of 6 next to
// "something is stuck on our side" reads as progress, and the point of the
// escalation tiers (issue #1468) is that past a certain point this UI stops
// claiming any. Those two keep the flat AGENTS_BANNER_COPY sentence and their
// actions, unchanged — and so does any phase we can't place on the ladder.
const ADVANCING_BANNER_COPY: Partial<Record<keyof typeof AGENTS_BANNER_COPY, string>> = {
  starting: "Your team will be ready in about a minute.",
  slow: AGENTS_BANNER_COPY.slow,
  welcome: AGENTS_BANNER_COPY.welcome,
};

/**
 * In-flow status line above the workspace while the container is not ready.
 * Escalates honestly (issue #1468): a slow start offers "Check again", a stuck
 * or failed one also offers support, and nothing here resets the clock.
 *
 * While it is still honest to claim progress, it shows which setup step is
 * actually running (its plain-language label + a bar), because
 * the alternative is one unchanging sentence for the 5+ minutes upstream's
 * sidecars.channels can take. The position comes from the real `phase`, so the
 * bar only moves when the backend actually moved.
 */
function AgentsBanner({
  state,
  phase,
  onCheck,
  onSupport,
  isChecking,
  checkFailed,
}: {
  state: keyof typeof AGENTS_BANNER_COPY;
  phase: Phase;
  onCheck: () => void;
  onSupport: () => void;
  /** True while a `failed`-state Retry's /container/recover call is in flight. */
  isChecking: boolean;
  /** True when that recover call itself errored (not just "nothing to recover"). */
  checkFailed: boolean;
}) {
  const alarming = state === "stuck" || state === "failed";
  const progress = state in ADVANCING_BANNER_COPY ? agentsBannerProgress(phase) : null;
  return (
    <div
      role="status"
      aria-live="polite"
      className={`order-first bg-cream border-b px-4 py-2 text-ui ${
        state === "failed" ? "border-err text-err" : alarming ? "border-warn text-warn" : "border-[var(--edge)] text-[var(--midnight-muted)]"
      }`}
    >
      <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1">
        <span className="flex flex-wrap items-baseline gap-x-2">
          {progress && (
            <span className="font-medium text-[var(--midnight)]">{progress.activeLabel}</span>
          )}
          <span>{(progress && ADVANCING_BANNER_COPY[state]) || AGENTS_BANNER_COPY[state]}</span>
          {state === "failed" && checkFailed && (
            <span className="text-err">Retry failed — try again or contact support.</span>
          )}
        </span>
        {state !== "starting" && state !== "welcome" && (
          <span className="flex gap-4">
            <button type="button" className="underline disabled:opacity-60" onClick={onCheck} disabled={isChecking}>
              {isChecking ? "Retrying…" : state === "failed" ? "Retry" : "Check again"}
            </button>
            {alarming && (
              <a className="underline" href={SUPPORT_MAILTO} onClick={onSupport}>Contact support</a>
            )}
          </span>
        )}
      </div>
      {progress && (
        <div className="mt-1.5 flex items-center gap-3">
          {/* One bar, one live position. aria-valuetext carries the step name
              so a screen reader gets "Introducing your team…" rather than a
              bare "67%" with no idea what moved. */}
          <div
            role="progressbar"
            aria-label="Agent setup progress"
            aria-valuemin={0}
            aria-valuemax={progress.total}
            aria-valuenow={progress.step}
            aria-valuetext={progress.activeLabel}
            className="h-1 flex-1 overflow-hidden rounded-full bg-[var(--edge)]"
          >
            <div
              className="h-full rounded-full bg-[var(--midnight)] transition-[width] duration-700 ease-out motion-reduce:transition-none"
              style={{ width: `${(progress.step / progress.total) * 100}%` }}
            />
          </div>
        </div>
      )}
    </div>
  );
}

/**
 * Non-blocking "Teams needs attention" affordance layered over the ready app
 * when /container/status reports `teams_state: "failed"` — Teams (Friday/Atlas)
 * seeding failed but the container is healthy and chat works. Small and
 * dismissible so it never blocks the app. Owns its own dismiss state so the
 * ready branch stays a plain `children` return with no extra parent hook.
 * PLACEMENT belongs to the ready branch's corner stack, not to this card — see
 * the comment there.
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
      className="max-w-xs rounded-xl border border-warn bg-cream px-4 py-3 shadow-lg"
    >
      <div className="flex items-start gap-3">
        <div className="flex-1">
          <p className="text-sm font-medium text-warn">Teams needs attention</p>
          <p className="mt-1 text-xs text-[var(--midnight-soft)]">
            Your team workspace didn&apos;t finish setting up, so some team
            features may be unavailable. Chat works normally.
          </p>
          <a
            href={SUPPORT_MAILTO}
            onClick={() =>
              capture("support_contact_clicked", { source: "teams_degraded_notice" })
            }
            className="mt-2 inline-block text-xs font-medium text-[var(--midnight)] underline"
          >
            Contact support
          </a>
        </div>
        <button
          type="button"
          aria-label="Dismiss"
          onClick={() => setDismissed(true)}
          className="text-lg leading-none text-[var(--midnight-muted)] hover:text-[var(--midnight)]"
        >
          ×
        </button>
      </div>
    </div>
  );
}


/**
 * The provider comparison shown before subscription.
 *
 * Checkout creates one workspace subscription; it deliberately does not
 * persist a provider choice. Provider setup is a post-entitlement step.
 */

export function ProviderPicker({
  isOrg,
  orgName,
  heading,
  subheading,
  source = "provider_picker",
  embedded = false,
}: {
  isOrg: boolean;
  orgName?: string;
  heading?: string;
  subheading?: string;
  source?: string;
  /**
   * Render inside an existing page that already has its own chrome (the
   * settings Billing panel) rather than as the full-bleed onboarding screen:
   * drops the logo and the centered full-height wrapper, keeps the cards.
   */
  embedded?: boolean;
}) {
  // The checkout call lives in useBilling, shared with the settings Billing
  // panel. It used to be duplicated inline here — two copies of the same POST
  // + redirect, which is how the two surfaces drifted into offering different
  // copy for the same three paths in the first place.
  const { startCheckout, account } = useBilling();
  const workspacePriceUsd = account?.workspace_price_usd;
  const workspacePriceDescription =
    workspacePriceUsd === undefined
      ? "We'll show your workspace subscription price shortly."
      : `The $${workspacePriceUsd}/month workspace subscription covers your always-on agent infrastructure.`;
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const comparisonCards = providerComparisonCards(
    account?.is_subscribed === true && account?.managed_inference_available === true,
  );
  const startWorkspaceCheckout = useCallback(async () => {
    setLoading(true);
    setError(null);
    capture("subscription_checkout_started", {
      owner_type: isOrg ? "org" : "personal",
      source,
    });
    try {
      // Checkout does not carry a provider choice. The provider controls are
      // reached after entitlement, through the existing provider flow.
      await startCheckout("workspace");
    } catch (err) {
      capture("subscription_checkout_failed", {
        owner_type: isOrg ? "org" : "personal",
        source,
        error: err instanceof Error ? err.message : "unknown",
      });
      setLoading(false);
      // Fixed copy, not err.message. startCheckout propagates ApiError
      // carrying the backend `detail`, so surfacing it verbatim put strings
      // like "No Stripe customer for owner_id=user_2xK…" and the literal
      // "checkout_in_progress" in front of a customer at the paywall. The
      // real error still reaches the console and the capture() above.
      console.error("workspace checkout failed", err);
      setError("Couldn't start checkout. Please try again.");
    }
  }, [startCheckout, isOrg, source]);

  return (
    // `@container` (not a viewport breakpoint) because this now renders inside
    // a resizable dock panel. The grid below used `md:grid-cols-3`, which keys
    // off the VIEWPORT: on a wide desktop with a narrow chat panel that forces
    // three cards into a ~400px column regardless of how little room they have.
    // Sized on the container instead, the same component stacks in the chat
    // transcript and goes three-wide in the settings card and the full-screen
    // onboarding step.
    <div className={"@container " + (embedded ? "" : "flex-1 flex items-center justify-center p-8 bg-[var(--cream)]")}>
      <div className={"w-full space-y-8 " + (embedded ? "" : "max-w-5xl text-center")}>
        <div className="space-y-3">
          {!embedded && (
            <LogoMark className="brand-mark--hero mb-4" />
          )}
          <h2 className="text-2xl font-semibold tracking-display text-[var(--midnight)] font-editorial-serif">
            {heading ?? "One workspace plan"}
          </h2>
          <p className={"text-[var(--midnight-muted)] text-sm max-w-md " + (embedded ? "" : "mx-auto")}>
            {subheading ??
              (isOrg
                ? `${workspacePriceDescription} Choose your inference provider after checkout.`
                : `${workspacePriceDescription} Choose your inference provider after checkout.`)}
          </p>
        </div>

        {error && (
          <div className="text-sm text-err -mt-4">{error}</div>
        )}

        <div className={`grid grid-cols-1 gap-4 ${comparisonCards.length >= 3 ? "@3xl:grid-cols-3" : "@3xl:grid-cols-2"}`}>
          {comparisonCards.map((card) => (
            <div
              key={card.id}
              className={
                "rounded-xl border p-6 space-y-4 bg-cream text-left flex flex-col " +
                (card.highlighted ? "border-2 border-[var(--midnight)]" : "border-[var(--edge)]")
              }
            >
              <div className="space-y-1">
                <h3 className="font-medium text-[var(--midnight)]">{card.title}</h3>
                <p className="text-sm text-[var(--midnight-muted)]">
                  {workspacePriceUsd === undefined
                    ? "Loading workspace price…"
                    : `$${workspacePriceUsd} / month`}
                </p>
                <p className="text-xs text-[var(--midnight)] font-medium">
                  {card.subtitle}
                </p>
                <p className="text-xs text-[var(--midnight-muted)]">
                  {card.trialNote}
                </p>
                {isOrg && card.id === "chatgpt_oauth" && (
                  <p className="text-xs text-[var(--midnight-muted)]">
                    You connect your ChatGPT account (Pro recommended) — one
                    account powers the whole workspace.
                  </p>
                )}
              </div>
              <ul className="text-sm text-[var(--midnight-soft)] space-y-2 flex-1">
                {card.bullets.map((b) => (
                  <li key={b} className="flex items-start gap-2">
                    <span aria-hidden className="text-[var(--midnight)]">✓</span>
                    <span>{b}</span>
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </div>
        <div className={embedded ? "" : "mx-auto max-w-sm"}>
          {isOrg ? (
            <Button
              className="w-full rounded-full bg-[var(--midnight)] hover:bg-[var(--midnight-soft)] text-cream"
              onClick={startWorkspaceCheckout}
              disabled={loading}
            >
              {loading ? <Loader2 className="h-4 w-4 animate-spin" /> : "Subscribe to workspace plan"}
            </Button>
          ) : (
            <p className="text-sm text-[var(--midnight-muted)]">
              Create or select a workspace to subscribe.
            </p>
          )}
        </div>
      </div>
    </div>
  );
}
