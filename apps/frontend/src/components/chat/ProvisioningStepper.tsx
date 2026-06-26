"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";

import { capture } from "@/lib/analytics";
import {
  Loader2,
  XCircle,
  CheckCircle,
  Circle,
} from "lucide-react";
import { useOrganization } from "@clerk/nextjs";
import { Button } from "@/components/ui/button";
import { useApi } from "@/lib/api";
import { useBilling } from "@/hooks/useBilling";
import { useProvisioningState, type ColdStartPhase } from "@/hooks/useProvisioningState";
import { useGateway } from "@/hooks/useGateway";
import { useGatewayRpc } from "@/hooks/useGatewayRpc";
import { nextOnboardingCompletion } from "@/components/chat/onboardingAnalytics";
import { ChatGPTOAuthStep } from "@/components/chat/ChatGPTOAuthStep";
import { ByoKeyStep } from "@/components/chat/ByoKeyStep";
import { CreditsStep } from "@/components/chat/CreditsStep";
import { Logo } from "@/components/Logo";
import { PROVIDER_CARDS, type ProviderChoiceId } from "@/lib/providerCards";

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

type ProviderChoice = "chatgpt_oauth" | "byo_key" | "bedrock_claude";

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

// Fargate cold starts (image pull from ECR + ENI attach + container init) can
// legitimately run 3-4 minutes for the first task on a service. Anything
// shorter cries wolf during the normal happy path. The timer also doesn't
// start until we actually enter the "container" phase — see provisioningStart
// below — so time spent on OAuth/billing/credits doesn't burn this budget.
const TIMEOUT_MS = 300_000;

/**
 * Rotating idea prompts shown while the container spins up. Kept short (≤ ~52 chars)
 * so they fit on one line and don't cause layout shift. Tone matches the rest of the
 * onboarding copy: concrete, action-oriented, a little aspirational.
 */
const PROVISION_IDEAS: readonly string[] = [
  "Draft your weekly status report from your inbox",
  "Triage email and reply in your voice",
  "Summarize yesterday's meetings into action items",
  "Spin up a research brief on any topic",
  "Schedule focus time around your calendar",
  "Turn a messy doc into a clean one-pager",
  "Watch a channel and ping you on what matters",
  "Run a recurring morning briefing for you",
  "Pull metrics and drop them into a shared doc",
  "Review a PR and flag the risky changes",
];

const IDEA_ROTATION_MS = 3200;

function formatElapsed(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${m}:${s.toString().padStart(2, "0")}`;
}

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
  const { account, isLoading: billingLoading, isSubscribed } = useBilling();
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
  const router = useRouter();
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
    const remaining = params.toString();
    router.replace(`/chat${remaining ? `?${remaining}` : ""}`, { scroll: false });
  }, [providerStepDone, searchParams, router]);

  // Which provider the user just finished in THIS session, captured at
  // completion so the "finishing" copy stays specific even after ?provider= is
  // stripped from the URL above. Null until the step completes; resets on remount.
  const [completedProvider, setCompletedProvider] = useState<ProviderChoice | null>(null);

  // Open a grace window when the provider step completes. While it is open and
  // the gate is still blocked, the settling loader below wins over the cold
  // gate card; once it lapses the real gate card takes over again. The dep is
  // the rising edge of providerStepDone (false -> true), which fires once.
  const [providerSettling, setProviderSettling] = useState(false);
  useEffect(() => {
    if (!providerStepDone) return;
    setProviderSettling(true);
    const timer = setTimeout(() => setProviderSettling(false), PROVIDER_SETTLE_MS);
    return () => clearTimeout(timer);
  }, [providerStepDone]);

  const [reachedReady, setReachedReady] = useState(false);
  const shouldPollContainer = isSubscribed && !reachedReady;

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
    refreshInterval: shouldPollContainer ? 3000 : 0,
  });

  useEffect(() => {
    if (container?.phase === "ready" && !reachedReady) setReachedReady(true);
  }, [container?.phase, reachedReady]);

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

  // Provisioning is now driven by /container/status: when the gate clears
  // and no container exists, the backend's status endpoint fires
  // ensure_user_ready itself. The frontend stepper is purely passive — it
  // polls /container/status (every 3s during pre-ready) and renders the
  // appropriate phase. No explicit POST /container/provision from here.

  const containerReady = container?.status === "running" || container?.substatus === "gateway_healthy";

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
    previousBackendPhaseRef.current === "ready" &&
    container?.phase !== undefined &&
    container.phase !== "ready";
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
    // Recovery flow skips billing — the user already has a subscription.
    if (trigger !== "recovery" && !isSubscribed) return "payment";
    // Provider-specific signup step runs before container provision.
    if (needsProviderStep) return "provider";

    // Trust the backend's phase field when it's present; fall back to
    // the legacy status check for older backends still in flight.
    const backendPhase = container?.phase;

    if (!container) return "container";
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
  }, [trigger, isSubscribed, container, containerReady, gatewayHealth, needsProviderStep, isRegressingFromReady]);

  // Analytics: fire `onboarding_step_completed` once per step transition.
  const prevPhaseRef = useRef<Phase | null>(null);
  useEffect(() => {
    const prev = prevPhaseRef.current;
    prevPhaseRef.current = phase;
    if (prev === null || prev === phase) return;
    const completion = nextOnboardingCompletion(prev, phase, STEPS_PAID);
    if (completion) capture("onboarding_step_completed", { ...completion });
  }, [phase]);

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

  // Ready — render children
  if (phase === "ready") {
    return <>{children}</>;
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
                  <a href="mailto:support@isol8.co">Contact support</a>
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
  // provider_choice is NOT persisted via /users/sync here. The
  // ProviderPicker's "Subscribe" step writes it synchronously to
  // billing_accounts via POST /billing/trial-checkout, which is the
  // canonical (and only) write path. billing_repo (owner-keyed) is the
  // source of truth.
  if (phase === "provider" && providerChoice !== null) {
    const handleProviderComplete = async () => {
      capture("onboarding_provider_completed", {
        provider_choice: providerChoice,
      });
      setCompletedProvider(providerChoice);
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
                  <a href="mailto:support@isol8.co">Contact support</a>
                </Button>
              </div>
            </div>
          </div>
        </div>
      </>
    );
  }

  // Loading billing
  if (billingLoading) {
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
  const ownerOnboarded = isSubscribed && Boolean(account?.provider_choice);
  if (!ownerOnboarded) {
    return <ProviderPicker isOrg={isOrg} orgName={organization?.name} />;
  }

  // Error state
  if (container?.status === "error") {
    return (
      <div className="flex-1 flex items-center justify-center bg-[#faf7f2]">
        <div className="text-center space-y-6 max-w-sm">
          <StepperDisplay currentPhase={phase} steps={STEPS_PAID} error />
          <div className="space-y-2">
            <XCircle className="h-8 w-8 text-[#dc2626] mx-auto" />
            <h2 className="text-lg font-medium text-[#1a1a1a]">Setup failed</h2>
            <p className="text-sm text-[#8a8578]">
              {isOrg
                ? `Something went wrong while setting up the container for ${organization.name}. This is usually temporary.`
                : "Something went wrong while setting up your container. This is usually temporary."}
            </p>
          </div>
          <div className="flex gap-3 justify-center">
            <Button variant="outline" className="rounded-full border-[#e0dbd0]" onClick={() => refresh()}>
              Retry
            </Button>
            <Button variant="ghost" className="rounded-full" asChild>
              <a href="mailto:support@isol8.co">Contact Support</a>
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
                  <a href="mailto:support@isol8.co">Contact support</a>
                </Button>
              </div>
            </div>
          )}
        </div>
      </div>
    </>
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
      setLoading(null);
      setError(err instanceof Error ? err.message : "Couldn't start checkout. Please try again.");
    }
  }, [api]);

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
