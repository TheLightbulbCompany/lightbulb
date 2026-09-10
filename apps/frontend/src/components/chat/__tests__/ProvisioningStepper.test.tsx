import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { SWRConfig } from "swr";
import { render, screen, act, fireEvent } from "@testing-library/react";
import React from "react";
import { PROVISION_STUCK_MS, PROVISION_TIMEOUT_MS } from "@lightbulb/shared";

// Global-mutate spy for the ready re-kick (the stepper sweeps `rpc|*` SWR
// keys via useSWRConfig().mutate on the not-ready→ready transition). Partial
// mock: everything else in `swr` stays real for any hook in the tree.
const { swrGlobalMutate } = vi.hoisted(() => ({ swrGlobalMutate: vi.fn() }));
vi.mock("swr", async (importOriginal) => {
  const actual = await importOriginal<typeof import("swr")>();
  return {
    ...actual,
    useSWRConfig: () => ({ mutate: swrGlobalMutate }),
  };
});

vi.mock("@/hooks/useLiveEvents", () => ({
  useLiveEvents: () => ({
    liveConnected: true,
    onEvent: () => () => {},
  }),
}));

// The blocked-state branch in ProvisioningStepper renders before any of the
// container/billing/orchestration logic, so we only need the hooks the
// component touches *up to* that early-return to be functional. Everything
// else gets a no-op stub.

vi.mock("@/hooks/useProvisioningState", () => ({
  useProvisioningState: vi.fn(),
}));

// Configurable so the onboarding-status gate tests can exercise the
// `user` present branch — most other tests in this file rely on the
// default `null` (which makes the first-run funnel branch unreachable and
// keeps every pre-existing test's assertions about phase="ready" etc.
// untouched).
let clerkMockUser: {
  id: string;
  unsafeMetadata: { firstRun?: { briefSubmitted?: boolean } };
} | null = null;
// Configurable so the user-flash test can exercise the "Clerk still resolving"
// (isLoaded=false) branch; every other test keeps the default `true`.
let clerkMockIsLoaded = true;
// Configurable so the per-container onboarding-gate tests can exercise an ORG
// owner (the reported "paid org owner never sees the funnel" scenario). The
// funnel gate is org-agnostic, so most tests keep the default `null` (personal).
let clerkMockOrganization: { id: string; name: string } | null = null;
vi.mock("@clerk/nextjs", () => ({
  useOrganization: () => ({ organization: clerkMockOrganization, membership: null, isLoaded: true }),
  // Components in the stepper tree (useApi consumers) read useAuth/useUser.
  useAuth: () => ({
    getToken: vi.fn().mockResolvedValue("test-token"),
    isSignedIn: true,
  }),
  useUser: () => ({ user: clerkMockUser, isLoaded: clerkMockIsLoaded }),
}));

// ``lib/analytics`` re-exports PostHogProvider as ``Provider`` at module
// load time, so the mock has to expose it (vitest is strict about
// import-time destructuring against module mocks). Returning a passthrough
// component is enough — these tests don't render through the real provider.
vi.mock("posthog-js/react", () => ({
  usePostHog: () => null,
  PostHogProvider: ({ children }: { children: React.ReactNode }) => children,
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
  useSearchParams: vi.fn(() => new URLSearchParams()),
}));

// Stable mocks so individual tests can assert against `apiPost` etc. The
// previous implementation built a fresh `vi.fn()` per `useApi()` call, which
// meant `expect(post).not.toHaveBeenCalled()` couldn't be wired up — every
// render returned a brand-new spy.
const apiPost = vi.fn().mockResolvedValue({});
const apiGet = vi.fn().mockResolvedValue({});
const apiPut = vi.fn().mockResolvedValue({});
const apiDel = vi.fn().mockResolvedValue({});

vi.mock("@/lib/api", () => ({
  useApi: () => ({
    // ProvisioningStepper chains .catch on the result, so the mock must
    // return a promise — vi.fn() defaults to undefined which crashes.
    post: apiPost,
    get: apiGet,
    put: apiPut,
    del: apiDel,
  }),
  // useChatGPTOAuth (transitively imported via ChatGPTOAuthStep) does
  // `e instanceof ApiError` in its 409 handler — the mock must expose a
  // matching class or the import resolves to undefined and throws at runtime.
  ApiError: class ApiError extends Error {
    public readonly status: number;
    public readonly body: unknown;
    constructor(status: number, body: unknown, message?: string) {
      super(message ?? `API ${status}`);
      this.status = status;
      this.body = body;
    }
  },
}));

// Configurable so the trial_local (free-tier) test can flip isSubscribed /
// account shape without a persisted provider_choice.
let billingMockIsSubscribed = true;
let billingMockAccount: {
  provider_choice: string | null;
  subscription_status?: string | null;
  workspace_price_usd?: number;
} | null = {
  provider_choice: "bedrock_claude",
};
// Spy for the /billing/account re-fetch the stepper fires when a container is
// provisioning but billing hasn't caught up to the just-created trial_local.
let billingMockRefresh = vi.fn();
// Spy for the workspace-billing checkout CTA.
const billingMockStartCheckout = vi.fn();
vi.mock("@/hooks/useBilling", () => ({
  // Default: provider_choice set, an onboarded owner (the cold-start tests
  // need to get PAST the ProviderPicker gate, which requires isSubscribed AND
  // a persisted provider choice).
  useBilling: () => ({
    isLoading: false,
    isSubscribed: billingMockIsSubscribed,
    account: billingMockAccount,
    refresh: billingMockRefresh,
    startCheckout: billingMockStartCheckout,
  }),
}));

// Configurable so cold-start tests can flip the gateway `health` RPC
// between unanswered (null) and answered.
let gatewayHealthData: Record<string, unknown> | null = null;
vi.mock("@/hooks/useRuntimeRpc", () => ({
  useRuntimeRpc: () => ({ data: gatewayHealthData, error: null }),
}));

vi.mock("@/lib/analytics", () => ({
  capture: vi.fn(),
}));

import { useProvisioningState } from "@/hooks/useProvisioningState";
import { ProvisioningStepper } from "../ProvisioningStepper";

// The cold-start stepper render path queries prefers-reduced-motion;
// jsdom has no matchMedia.
window.matchMedia =
  window.matchMedia ||
  ((query: string) =>
    ({
      matches: false,
      media: query,
      onchange: null,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      addListener: vi.fn(),
      removeListener: vi.fn(),
      dispatchEvent: vi.fn(),
    }) as unknown as MediaQueryList);

const useProvMock = useProvisioningState as ReturnType<typeof vi.fn>;

describe("ProvisioningStepper blocked-state rendering", () => {
  beforeEach(() => {
    useProvMock.mockReset();
    apiPost.mockClear();
    apiGet.mockClear();
    apiPut.mockClear();
    apiDel.mockClear();
    billingMockIsSubscribed = true;
    billingMockAccount = { provider_choice: "bedrock_claude" };
    billingMockRefresh = vi.fn();
    billingMockStartCheckout.mockClear();
    clerkMockUser = null;
    clerkMockIsLoaded = true;
  });

  afterEach(() => {
    // beforeEach's mockClear resets call history but not the implementation, so
    // a test that overrides apiGet's resolved value would leak it into later
    // tests if it threw before restoring. Reset the default here unconditionally.
    apiGet.mockResolvedValue({});
  });

  it("derives cold-start sub-phases from real signals, not elapsed time", () => {
    // substatus task_running = ECS task up, gateway port still closed.
    const base = {
      phase: "normal",
      blocked: null,
      refreshInterval: 3000,
      refresh: vi.fn(),
    };
    useProvMock.mockReturnValue({
      ...base,
      container: {
        container_status: "provisioning",
        runtime_phase: "provisioning",
        container_substatus: "task_running",
      },
    });
    const { rerender, unmount } = render(
      <ProvisioningStepper>
        <div>children</div>
      </ProvisioningStepper>,
    );
    expect(screen.getByText("Connecting to your agent's brain...")).toBeInTheDocument();

    // Earlier provisioning (no task yet) stays on the container step.
    useProvMock.mockReturnValue({
      ...base,
      container: {
        container_status: "provisioning",
        runtime_phase: "provisioning",
        container_substatus: "service_created",
      },
    });
    rerender(
      <ProvisioningStepper>
        <div>children</div>
      </ProvisioningStepper>,
    );
    expect(screen.getByText("Provisioning your container...")).toBeInTheDocument();

    // phase=starting (gateway port open) with the health RPC unanswered:
    // OpenClaw is booting channels/sidecars.
    useProvMock.mockReturnValue({
      ...base,
      container: {
        container_status: "running",
        runtime_phase: "starting",
        container_substatus: "gateway_healthy",
      },
    });
    rerender(
      <ProvisioningStepper>
        <div>children</div>
      </ProvisioningStepper>,
    );
    expect(screen.getByText("Wiring up your channels...")).toBeInTheDocument();
    unmount();

    // Health RPC answers but the pool handshake hasn't completed: the
    // agent step (fresh mount so the regression-reset path can't interfere).
    gatewayHealthData = { ok: true };
    useProvMock.mockReturnValue({
      ...base,
      container: {
        container_status: "running",
        runtime_phase: "starting",
        container_substatus: "gateway_healthy",
      },
    });
    render(
      <ProvisioningStepper>
        <div>children</div>
      </ProvisioningStepper>,
    );
    expect(
      screen.getByText("Putting the finishing touches on your agent..."),
    ).toBeInTheDocument();
    gatewayHealthData = null;
  });

  it("renders title + message + action button when admin and not admin_only", () => {
    useProvMock.mockReturnValue({
      phase: "blocked",
      container: null,
      blocked: {
        code: "credits_required",
        title: "Top up Claude credits to start your container",
        message: "Top up some Claude credits to start your Bedrock container.",
        action: {
          kind: "link",
          label: "Top up now",
          href: "/settings/billing#credits",
          admin_only: false,
        },
        owner_role: "admin",
      },
      refreshInterval: 5000,
      refresh: vi.fn(),
    });

    render(
      <ProvisioningStepper>
        <div>children</div>
      </ProvisioningStepper>,
    );

    expect(
      screen.getByText(/Top up Claude credits to start your container/),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/Top up some Claude credits to start your Bedrock container\./),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Top up now/ })).toHaveAttribute(
      "href",
      "/settings/billing#credits",
    );
    // Member-only fallback copy must NOT appear when the action is allowed.
    expect(screen.queryByText(/Ask your org admin/i)).not.toBeInTheDocument();
  });

  it("renders 'ask your admin' when member-and-admin_only", () => {
    useProvMock.mockReturnValue({
      phase: "blocked",
      container: null,
      blocked: {
        code: "subscription_required",
        title: "Subscribe to start your container",
        message: "An active subscription is required.",
        action: {
          kind: "link",
          label: "Subscribe",
          href: "/onboarding",
          admin_only: true,
        },
        owner_role: "member",
      },
      refreshInterval: 5000,
      refresh: vi.fn(),
    });

    render(
      <ProvisioningStepper>
        <div>children</div>
      </ProvisioningStepper>,
    );

    expect(screen.getByText(/Ask your org admin/i)).toBeInTheDocument();
    expect(
      screen.queryByRole("link", { name: /Subscribe/ }),
    ).not.toBeInTheDocument();
    // Title and message still render even when the action is hidden.
    expect(
      screen.getByText(/Subscribe to start your container/),
    ).toBeInTheDocument();
    expect(screen.getByText(/An active subscription is required\./)).toBeInTheDocument();
  });

  it("wires the 'Check again' button to the hook's refresh()", () => {
    const refresh = vi.fn();
    useProvMock.mockReturnValue({
      phase: "blocked",
      container: null,
      blocked: {
        code: "credits_required",
        title: "Top up Claude credits to start your container",
        message: "Top up some Claude credits to start your Bedrock container.",
        action: {
          kind: "link",
          label: "Top up now",
          href: "/settings/billing#credits",
          admin_only: false,
        },
        owner_role: "admin",
      },
      refreshInterval: 5000,
      refresh,
    });

    render(
      <ProvisioningStepper>
        <div>children</div>
      </ProvisioningStepper>,
    );

    const checkAgain = screen.getByRole("button", { name: /Check again/ });
    checkAgain.click();
    expect(refresh).toHaveBeenCalled();
  });

  it("renders TeamBillingCard (not the generic blocked card) for team_billing_required", async () => {
    billingMockAccount = { provider_choice: null, workspace_price_usd: 50 };
    useProvMock.mockReturnValue({
      phase: "blocked",
      container: null,
      blocked: {
        code: "team_billing_required",
        title: "Subscribe to start your team's containers",
        message: "Your organization needs a Teams subscription.",
        action: {
          kind: "link",
          label: "Subscribe",
          href: "/settings/billing",
          admin_only: true,
        },
        owner_role: "admin",
      },
      refreshInterval: 5000,
      refresh: vi.fn(),
    });

    render(
      <ProvisioningStepper>
        <div>children</div>
      </ProvisioningStepper>,
    );

    // The workspace card, not the generic gate copy.
    expect(screen.getByText(/unlimited members/i)).toBeInTheDocument();
    expect(screen.queryByText(/Subscribe to start your team's containers/)).not.toBeInTheDocument();

    await act(async () => {
      screen.getByRole("button", { name: /subscribe workspace/i }).click();
    });
    expect(billingMockStartCheckout).toHaveBeenCalledWith("workspace");

    // The card must carry its own chrome: `.settings-*` classes live in a
    // route-scoped stylesheet /chat never loads, so relying on them here is
    // what made this gate paint as raw unstyled text in prod.
    const card = screen.getByTestId("team-billing-card");
    expect(card.className).not.toMatch(/settings-/);
    expect(card.className).toMatch(/bg-cream(?![-\w])/);
    // Same centerpiece footer the generic blocked card gets.
    expect(screen.getByRole("button", { name: /Check again/ })).toBeInTheDocument();
  });

  it.each([["trial_daily_cap"], ["trial_expired"]])(
    "renders the provider picker in place (not a link to settings) for %s",
    (code) => {
      // A capped/expired free-tier owner converts RIGHT HERE. The generic
      // blocked card's action is a link out to /settings?panel=billing, which
      // landed on a lone Subscribe button posting a HARDCODED
      // `provider_choice: "bedrock_claude"` — so the owner never chose a
      // provider, and (because no `?provider=` came back from Checkout) never
      // reached CreditsStep either. They ended up on Bedrock-managed inference
      // having chosen nothing and bought nothing.
      useProvMock.mockReturnValue({
        phase: "blocked",
        container: null,
        blocked: {
          code,
          title: "Daily free limit reached",
          message: "You've used today's free allowance — resets tomorrow.",
          action: {
            kind: "link",
            label: "Upgrade",
            href: "/settings?panel=billing",
            admin_only: false,
          },
          owner_role: "admin",
        },
        refreshInterval: 5000,
        refresh: vi.fn(),
      });

      render(
        <ProvisioningStepper>
          <div>children</div>
        </ProvisioningStepper>,
      );

      // All three paths offered — including buying Claude credits, which is
      // what the bedrock_claude card threads through to CreditsStep.
      expect(screen.getByText("Powered by Claude")).toBeInTheDocument();
      expect(screen.getByText("Sign in with ChatGPT")).toBeInTheDocument();
      expect(screen.getByText("Bring your own API key")).toBeInTheDocument();

      // The gate's message is the heading, rendered EXACTLY ONCE. It used to
      // also be concatenated into the subheading under a heading derived from
      // the same text, so the paywall showed the sentence twice — at the exact
      // moment someone is deciding to pay.
      expect(screen.getAllByText(/You've used today's free allowance/)).toHaveLength(1);
      expect(screen.getByText(/Choose your inference provider after checkout/)).toBeInTheDocument();

      // And emphatically NOT the send-them-to-settings card.
      expect(
        screen.queryByRole("link", { name: /Upgrade/ }),
      ).not.toBeInTheDocument();
    },
  );

  it("hides the team CTA for a member when the gate says the action is admin_only", () => {
    billingMockAccount = { provider_choice: null, workspace_price_usd: 50 };
    useProvMock.mockReturnValue({
      phase: "blocked",
      container: null,
      blocked: {
        code: "team_billing_required",
        title: "Set up Teams billing",
        message: "Your team needs an active per-seat subscription to start.",
        action: {
          kind: "link",
          label: "Set up billing",
          href: "/settings/billing",
          admin_only: true,
        },
        owner_role: "member",
      },
      refreshInterval: 5000,
      refresh: vi.fn(),
    });

    render(
      <ProvisioningStepper>
        <div>children</div>
      </ProvisioningStepper>,
    );

    expect(screen.queryByRole("button", { name: /subscribe workspace/i })).toBeNull();
    expect(screen.getByText(/ask your workspace admin/i)).toBeInTheDocument();
  });

  it("renders the ProviderPicker when phase=needs-signup", () => {
    // Backend signal for "owner has never started signup": /status
    // returns 404, the hook surfaces it as phase="needs-signup", and
    // the stepper must render the picker (the only checkout entry
    // point). This must NOT depend on the post-billing-load
    // fall-through render — it should fire by intent.
    useProvMock.mockReturnValue({
      phase: "needs-signup",
      container: null,
      blocked: null,
      refreshInterval: 0,
      refresh: vi.fn(),
    });

    render(
      <ProvisioningStepper>
        <div>children</div>
      </ProvisioningStepper>,
    );

    // ProviderPicker headline + card titles confirm the picker rendered.
    expect(screen.getByRole("heading", { name: /One workspace plan/i })).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: /Sign in with ChatGPT/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: /Bring your own API key/i }),
    ).toBeInTheDocument();
  });

  it("bypasses the ProviderPicker while a container is provisioning even if billing lags the trial, and refreshes billing", () => {
    // Cross-fetch race: /container/status creates the trial_local row and
    // returns a provisioning container, but /billing/account was fetched
    // before the row existed so it still reads non-trial. The stepper must
    // trust the live container (not fall back to the picker) AND pull billing
    // once so isTrialLocal/isSubscribed catch up and cold-start polling
    // resumes.
    billingMockIsSubscribed = false;
    billingMockAccount = null;
    useProvMock.mockReturnValue({
      phase: "normal",
      container: { container_status: "provisioning", runtime_phase: "provisioning" },
      blocked: null,
      refreshInterval: 3000,
      refresh: vi.fn(),
    });

    render(
      <ProvisioningStepper>
        <div>children</div>
      </ProvisioningStepper>,
    );

    // Picker headline must be absent — the live container bypasses it.
    expect(screen.queryByText(/One workspace plan/i)).not.toBeInTheDocument();
    // Billing was re-fetched so the trial state can catch up.
    expect(billingMockRefresh).toHaveBeenCalled();
  });

  it("renders team-pending centerpiece when phase=team-pending", () => {
    // Backend Task 8: org members whose admin-owned workspace isn't
    // provisioned yet receive 202 from /container/status. The hook
    // surfaces phase=team-pending; the stepper must render a
    // "waiting on your admin" empty state with a Check again button.
    const refresh = vi.fn();
    useProvMock.mockReturnValue({
      phase: "team-pending",
      container: null,
      blocked: null,
      refreshInterval: 5_000,
      refresh,
    });

    render(
      <ProvisioningStepper>
        <div>children</div>
      </ProvisioningStepper>,
    );

    expect(
      screen.getByText(/Your team workspace is being set up/i),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/admin will finish this soon/i),
    ).toBeInTheDocument();

    const checkAgain = screen.getByRole("button", { name: /Check again/ });
    checkAgain.click();
    expect(refresh).toHaveBeenCalled();
  });

  it("does not POST /container/provision (canonical /container/status trigger handles it)", () => {
    // Phase=cold-starting + container=null is the exact state where the
    // pre-Task-10 stepper would have fired POST /container/provision. Now
    // the backend's /container/status endpoint owns provisioning kickoff
    // (Task 8), so the frontend stepper must remain purely passive.
    useProvMock.mockReturnValue({
      phase: "cold-starting",
      container: null,
      blocked: null,
      refreshInterval: 3000,
      refresh: vi.fn(),
    });

    render(
      <ProvisioningStepper>
        <div>children</div>
      </ProvisioningStepper>,
    );

    const provisionCalls = apiPost.mock.calls.filter(
      (args) => args[0] === "/container/provision",
    );
    expect(provisionCalls).toHaveLength(0);
  });

  it("skips ProviderPicker for a trial_local (free-tier) account and proceeds into provisioning", () => {
    // trial_local is the free-tier subscription_status: the backend already
    // treats it as an active subscription (isSubscribed=true), but these
    // users never set provider_choice. The naive
    // `isSubscribed && provider_choice` ownerOnboarded check would wrongly
    // fall through to the ProviderPicker mid-provision — the trap this test
    // guards against.
    billingMockIsSubscribed = true;
    billingMockAccount = { provider_choice: null, subscription_status: "trial_local" };

    useProvMock.mockReturnValue({
      phase: "normal",
      blocked: null,
      refreshInterval: 3000,
      refresh: vi.fn(),
      container: {
        container_status: "provisioning",
        runtime_phase: "provisioning",
        container_substatus: "service_created",
      },
    });

    render(
      <ProvisioningStepper>
        <div>children</div>
      </ProvisioningStepper>,
    );

    // Must NOT render the ProviderPicker.
    expect(screen.queryByText(/One workspace plan/i)).not.toBeInTheDocument();
    expect(
      screen.queryByRole("heading", { name: /Sign in with ChatGPT/i }),
    ).not.toBeInTheDocument();
    // Must proceed into the container cold-start phase, not "payment".
    expect(screen.getByText("Provisioning your container...")).toBeInTheDocument();
  });

  it("renders ChatGPTOAuthStep when ?provider=chatgpt_oauth even if gate is blocked", async () => {
    // After clicking the gate's "Sign in with ChatGPT" button, the URL
    // becomes /chat?provider=chatgpt_oauth. The gate is still firing
    // (oauth_required) until the user finishes signing in — but we
    // must render the provider step (which is the means by which the
    // gate gets cleared) instead of the gate UI on top of itself.
    const { useSearchParams } = await import("next/navigation");
    (useSearchParams as ReturnType<typeof vi.fn>).mockReturnValue(
      new URLSearchParams({ provider: "chatgpt_oauth" }),
    );

    useProvMock.mockReturnValue({
      phase: "blocked",
      container: null,
      blocked: {
        code: "oauth_required",
        title: "Sign in with ChatGPT",
        message: "Complete the ChatGPT sign-in to start your container.",
        action: {
          kind: "link",
          label: "Sign in with ChatGPT",
          href: "/chat?provider=chatgpt_oauth",
          admin_only: false,
        },
        owner_role: "admin",
      },
      refreshInterval: 5000,
      refresh: vi.fn(),
      error: undefined,
      isLoading: false,
    });

    render(
      <ProvisioningStepper>
        <div>children</div>
      </ProvisioningStepper>,
    );

    // Gate UI's title/button must NOT appear; ChatGPTOAuthStep renders.
    expect(
      screen.queryByText(/Complete the ChatGPT sign-in to start your container/),
    ).not.toBeInTheDocument();
  });

  it("holds a 'finishing' loader (not the cold restart card) right after the provider step completes while the gate is still catching up", async () => {
    // Repro of the post-sign-in flash: the device-code flow finishes and the
    // OAuth row is written, but /container/status hasn't observed it yet so the
    // gate still returns 402 oauth_required. Once ?provider= is stripped from
    // the URL the cold "Sign in with ChatGPT" restart card used to render —
    // telling a user who JUST signed in to do it again.
    const { useSearchParams } = await import("next/navigation");
    const search = useSearchParams as ReturnType<typeof vi.fn>;
    search.mockReturnValue(new URLSearchParams({ provider: "chatgpt_oauth" }));

    // The wizard's on-mount /status probe reports already-connected, so
    // ChatGPTOAuthStep lands in "completed" and fires onComplete →
    // setProviderStepDone(true).
    apiGet.mockResolvedValue({ connected: true, account_id: "acct_test" });

    useProvMock.mockReturnValue({
      phase: "blocked",
      container: null,
      blocked: {
        code: "oauth_required",
        title: "Sign in with ChatGPT",
        message: "Complete the ChatGPT sign-in to start your container.",
        action: {
          kind: "link",
          label: "Sign in with ChatGPT",
          href: "/chat?provider=chatgpt_oauth",
          admin_only: false,
        },
        owner_role: "admin",
      },
      refreshInterval: 5000,
      refresh: vi.fn(),
      error: undefined,
      isLoading: false,
    });

    const { rerender } = render(
      <ProvisioningStepper>
        <div>children</div>
      </ProvisioningStepper>,
    );

    // The settling loader appears once the wizard completes...
    expect(await screen.findByText(/Finishing ChatGPT sign-in/i)).toBeInTheDocument();

    // ...and once ?provider= is stripped (simulating the strip-effect, which
    // the router mock doesn't propagate back into useSearchParams), the cold
    // restart card still must NOT take over while the grace window is open.
    search.mockReturnValue(new URLSearchParams());
    rerender(
      <ProvisioningStepper>
        <div>children</div>
      </ProvisioningStepper>,
    );

    expect(await screen.findByText(/Finishing ChatGPT sign-in/i)).toBeInTheDocument();
    expect(
      screen.queryByRole("link", { name: "Sign in with ChatGPT" }),
    ).not.toBeInTheDocument();
  });

  it("shows the Setup-failed card for a TERMINAL error (status=error + substatus=provision_failed)", () => {
    // Honest contract: a real provisioning failure now arrives as
    // status="error". The terminal substatus must surface the "Setup failed"
    // card (Retry + Contact Support) — keyed off substatus, never `phase`
    // (which still resolves to "container" for an error row).
    useProvMock.mockReturnValue({
      phase: "normal",
      container: {
        container_status: "error",
        container_substatus: "provision_failed",
        runtime_phase: "provisioning",
        last_error: "ECS task failed to start",
      },
      blocked: null,
      refreshInterval: 3000,
      refresh: vi.fn(),
    });

    render(
      <ProvisioningStepper>
        <div>children</div>
      </ProvisioningStepper>,
    );

    expect(screen.getByText(/Setup failed/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Retry/i })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Contact Support/i })).toBeInTheDocument();
    // The app itself must not render behind the terminal card.
    expect(screen.queryByText("children")).not.toBeInTheDocument();
  });

  it("shows the Setup-failed card for a TERMINAL error with substatus=deployment_failed (rollout circuit breaker)", () => {
    // Terminal is any status="error" that isn't error_retrying — NOT just
    // provision_failed. The rollout circuit breaker emits deployment_failed;
    // it must show the card, not fall through to the plain spinner with no CTA.
    useProvMock.mockReturnValue({
      phase: "normal",
      container: {
        container_status: "error",
        container_substatus: "deployment_failed",
        runtime_phase: "provisioning",
        last_error: "deployment rolled back",
      },
      blocked: null,
      refreshInterval: 3000,
      refresh: vi.fn(),
    });

    render(
      <ProvisioningStepper>
        <div>children</div>
      </ProvisioningStepper>,
    );

    expect(screen.getByText(/Setup failed/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Retry/i })).toBeInTheDocument();
    expect(screen.queryByText("children")).not.toBeInTheDocument();
  });

  it("shows the Setup-failed card for a bare status=error with NO substatus (auto-retry gated / standing down)", () => {
    // When auto-retry is gated or standing down the backend emits a bare
    // status="error" with substatus=null. That is terminal by default and must
    // show the card — the exact endless-spinner regression the invert fixes.
    useProvMock.mockReturnValue({
      phase: "normal",
      container: {
        container_status: "error",
        container_substatus: null,
        runtime_phase: "provisioning",
        last_error: "retries exhausted",
      },
      blocked: null,
      refreshInterval: 3000,
      refresh: vi.fn(),
    });

    render(
      <ProvisioningStepper>
        <div>children</div>
      </ProvisioningStepper>,
    );

    expect(screen.getByText(/Setup failed/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Retry/i })).toBeInTheDocument();
    expect(screen.queryByText("children")).not.toBeInTheDocument();
  });

  it("keeps the provisioning stepper (NOT the Setup-failed card) for a RETRYING error", () => {
    // status="error" + substatus="error_retrying" is transient — the backend is
    // auto-retrying. The stepper must keep showing progress with a calm
    // "retrying" line; the terminal "Setup failed" card must NOT appear. This
    // is the whole point of the split: a dead container fails loudly, a
    // retrying one keeps showing progress.
    useProvMock.mockReturnValue({
      phase: "normal",
      container: {
        container_status: "error",
        container_substatus: "error_retrying",
        runtime_phase: "provisioning",
        last_error: "transient image pull error",
      },
      blocked: null,
      refreshInterval: 3000,
      refresh: vi.fn(),
    });

    render(
      <ProvisioningStepper>
        <div>children</div>
      </ProvisioningStepper>,
    );

    expect(screen.queryByText(/Setup failed/i)).not.toBeInTheDocument();
    expect(screen.getByText("Provisioning your container...")).toBeInTheDocument();
    expect(screen.getByText(/retrying automatically/i)).toBeInTheDocument();
  });

  it("renders the ready app plus a non-blocking Teams-degraded notice when teams_state=failed", () => {
    // teams_state is a contract field carrying a NON-BLOCKING signal: Teams
    // seeding failed but the container is healthy and chat works. The app
    // (children) must render,
    // with a small dismissible "Teams needs attention" affordance over it —
    // never a blocking interstitial.
    useProvMock.mockReturnValue({
      phase: "normal",
      container: {
        container_status: "running",
        runtime_phase: "ready",
        onboarded: true,
        teams_state: "failed",
      },
      blocked: null,
      refreshInterval: 3000,
      refresh: vi.fn(),
    });

    render(
      <ProvisioningStepper>
        <div>children</div>
      </ProvisioningStepper>,
    );

    // Chat is NOT blocked...
    expect(screen.getByText("children")).toBeInTheDocument();
    // ...and the degraded affordance is layered over it.
    expect(screen.getByText(/Teams needs attention/i)).toBeInTheDocument();
  });

  // Issue #1468: a first run that dies leaves the owner on an empty chat with
  // no explanation — the reason was computed, shipped on the contract, and
  // rendered ONLY on the admin page. These pin the owner-facing surface.
  describe("first-run (bootstrap) failure notice", () => {
    /** A failed first run with a controlled credit balance.
     *
     * Renders under its own SWR cache. The file resets `apiGet` between tests
     * but SWR's cache is global and `useCredits` dedupes for 30s, so an earlier
     * test's `/users/me` -> {} is served from cache and the mock below is never
     * consulted — the assertions then fail for a reason that has nothing to do
     * with the behaviour under test. */
    const renderFailedNoticeWithCredits = (balance: Record<string, unknown>) => {
      apiGet.mockImplementation((path: string) => {
        if (path === "/users/me") return Promise.resolve({ provider_choice: "bedrock_claude" });
        if (path === "/billing/credits/balance") return Promise.resolve(balance);
        return Promise.resolve({});
      });
      useProvMock.mockReturnValue({
        phase: "normal",
        container: {
          container_status: "running",
          runtime_phase: "ready",
          onboarded: true,
          funnel: { state: "submitted" },
          bootstrap: { state: "failed", last_error: "chat.error" },
        },
        blocked: null,
        refreshInterval: 3000,
        refresh: vi.fn(),
      });
      return render(
        <SWRConfig value={{ provider: () => new Map() }}>
          <ProvisioningStepper>
            <div>children</div>
          </ProvisioningStepper>
        </SWRConfig>,
      );
    };

    const readyWithBootstrap = (bootstrap: Record<string, unknown>) => {
      useProvMock.mockReturnValue({
        phase: "normal",
        container: {
          container_status: "running",
          runtime_phase: "ready",
          onboarded: true,
          funnel: { state: "submitted" },
          bootstrap,
        },
        blocked: null,
        refreshInterval: 3000,
        refresh: vi.fn(),
      });
      return render(
        <ProvisioningStepper>
          <div>children</div>
        </ProvisioningStepper>,
      );
    };

    it("renders the failure over the live chat, with the reason verbatim", () => {
      readyWithBootstrap({
        state: "failed",
        last_error:
          "openai isn't accepting your saved login (OAuth token refresh failed for openai: 401)",
      });

      // Chat is NOT blocked — the container is healthy and talking to Friday
      // directly is the recovery this card points at.
      expect(screen.getByText("children")).toBeInTheDocument();
      expect(screen.getByText(/Friday couldn't start/i)).toBeInTheDocument();
      // The raw reason is preserved, not swallowed — support needs it.
      expect(
        screen.getByText(/OAuth token refresh failed for openai/i),
      ).toBeInTheDocument();
    });

    it("offers support, never a Retry — the only re-arm lever is admin-only", () => {
      readyWithBootstrap({ state: "failed", last_error: "chat.error" });

      expect(
        screen.getByRole("link", { name: /contact support/i }),
      ).toHaveAttribute("href", "mailto:support@thelightbulb.company");
      // A Retry button here could not re-dispatch anything
      // (`claim_bootstrap_dispatch` admits only `not_started`, and
      // `reset_bootstrap_for_retry` is behind require_platform_admin) — a dead
      // button is the same bug in a new costume.
      expect(screen.queryByRole("button", { name: /retry/i })).not.toBeInTheDocument();
    });

    it("names credits as the blocker but NEVER removes the support exit", async () => {
      // `creditsBlocked` is read off the balance alone and never looks at
      // `lastError`, so it is a correlate and not a cause. An owner whose
      // container genuinely broke AND whose balance is low would be told a
      // falsehood — so the copy must not claim causation, and the one path to
      // a human must survive in both branches. Shipping the credits branch
      // without the support link is the bug this pins.
      renderFailedNoticeWithCredits({ balance_microcents: 0, balance_dollars: "0", is_exhausted: true });

      expect(await screen.findByText(/Your Claude credits are used up/i)).toBeInTheDocument();
      // Never asserts WHY the run died.
      expect(screen.queryByText(/couldn't start because/i)).not.toBeInTheDocument();
      expect(screen.getByRole("link", { name: /top up now/i })).toBeInTheDocument();
      expect(screen.getByRole("link", { name: /contact support/i })).toHaveAttribute(
        "href",
        "mailto:support@thelightbulb.company",
      );
    });

    it("keeps the support-only copy when credits are not the blocker", async () => {
      renderFailedNoticeWithCredits({
        balance_microcents: 5_000_000,
        balance_dollars: "5",
        is_exhausted: false,
      });

      expect(await screen.findByText(/her first run didn't get going/i)).toBeInTheDocument();
      expect(screen.queryByRole("link", { name: /top up now/i })).not.toBeInTheDocument();
      expect(screen.getByRole("link", { name: /contact support/i })).toBeInTheDocument();
    });

    it("still renders when the reason is missing (older row / degraded write)", () => {
      readyWithBootstrap({ state: "failed", last_error: null });

      expect(screen.getByText(/Friday couldn't start/i)).toBeInTheDocument();
      expect(screen.queryByText(/technical details/i)).not.toBeInTheDocument();
    });

    // The transient/recoverable rungs of the post-#1820 ladder. `dispatched`
    // (fresh OR stale-but-re-arming) and `sent` are both resolved by the
    // dispatcher itself on the next /container/status poll — rendering them as
    // failures would be a lie the owner can't act on.
    it.each([
      ["not_started", { state: "not_started", last_error: null }],
      ["dispatched (fresh)", { state: "dispatched", last_error: null }],
      [
        "dispatched (stale, re-arming — carries a last_error from the prior attempt)",
        { state: "dispatched", last_error: "connection refused" },
      ],
      ["sent", { state: "sent", last_error: null }],
      ["delivered", { state: "delivered", last_error: null }],
    ])("stays silent for bootstrap.state=%s", (_label, bootstrap) => {
      readyWithBootstrap(bootstrap);

      expect(screen.getByText("children")).toBeInTheDocument();
      expect(screen.queryByText(/Friday couldn't start/i)).not.toBeInTheDocument();
    });

    it("stacks with the Teams notice instead of covering it when both fire", () => {
      useProvMock.mockReturnValue({
        phase: "normal",
        container: {
          container_status: "running",
          runtime_phase: "ready",
          onboarded: true,
          teams_state: "failed",
          bootstrap: { state: "failed", last_error: "chat.error" },
        },
        blocked: null,
        refreshInterval: 3000,
        refresh: vi.fn(),
      });
      render(
        <ProvisioningStepper>
          <div>children</div>
        </ProvisioningStepper>,
      );

      // Both are readable. They used to place THEMSELVES at an identical
      // `fixed bottom-4 right-4`, which put one card directly on top of the
      // other; the ready branch's corner stack owns placement now.
      const firstRun = screen.getByText(/Friday couldn't start/i);
      const teams = screen.getByText(/Teams needs attention/i);
      expect(firstRun).toBeInTheDocument();
      expect(teams).toBeInTheDocument();
      expect(document.querySelectorAll(".fixed")).toHaveLength(1);
    });

    // THE COVERAGE HOLE. Every case above builds `runtime_phase: "ready"`,
    // which is the ONLY branch that read `bootstrap` — so the one state where
    // the owner has no chat surface to fall back on was untested and, in
    // production, unrendered. Observed on dev: 21 hours on the cold-start card
    // with `last_error` shipped on every poll and read by nothing.
    describe("...on a container that never reached ready", () => {
      // The exact observed shape: ECS RUNNING, gateway port answering, health
      // RPC answered, pool handshake never completed -> stepper phase "agent".
      const coldStartWithBootstrap = (bootstrap: Record<string, unknown> | undefined) => {
        gatewayHealthData = { ok: true };
        useProvMock.mockReturnValue({
          phase: "normal",
          container: {
            container_status: "running",
            runtime_phase: "starting",
            container_substatus: "gateway_healthy",
            onboarded: true,
            funnel: { state: "submitted" },
            ...(bootstrap ? { bootstrap } : {}),
          },
          blocked: null,
          refreshInterval: 3000,
          refresh: vi.fn(),
        });
        return render(
          <ProvisioningStepper>
            <div>children</div>
          </ProvisioningStepper>,
        );
      };
      afterEach(() => {
        gatewayHealthData = null;
      });

      it("shows the failure inline and drops every claim that it is still working", () => {
        coldStartWithBootstrap({
          state: "failed",
          last_error: "Gateway connect failed: gateway starting; retry shortly",
        });

        expect(screen.getByText(/Setup didn't finish/i)).toBeInTheDocument();
        expect(
          screen.getByText(/Gateway connect failed: gateway starting/i),
        ).toBeInTheDocument();

        // The three lies, gone: the step label, the caption, the idea prompts.
        expect(
          screen.queryByText("Putting the finishing touches on your agent..."),
        ).not.toBeInTheDocument();
        expect(screen.queryByText(/cold starts can take a few minutes/i)).not.toBeInTheDocument();
        expect(screen.queryByText(/While you wait/i)).not.toBeInTheDocument();
      });

      it("renders INLINE, never a second fixed overlay", () => {
        coldStartWithBootstrap({ state: "failed", last_error: "chat.error" });
        // The ready branch's corner stack is the only `.fixed` in this
        // component, and it is unreachable from here.
        expect(document.querySelectorAll(".fixed")).toHaveLength(0);
      });

      it("points at the live support address, and still offers no dead Retry", () => {
        coldStartWithBootstrap({ state: "failed", last_error: "chat.error" });
        expect(
          screen.getByRole("link", { name: /contact support/i }),
        ).toHaveAttribute("href", "mailto:support@thelightbulb.company");
        expect(screen.queryByRole("button", { name: /retry/i })).not.toBeInTheDocument();
      });

      // The non-lie rule has to hold on this branch too: `dispatched`/`sent`
      // are recovered by the dispatcher on the next poll, so a slow cold start
      // must keep looking like a slow cold start.
      it.each([
        ["not_started", { state: "not_started", last_error: null }],
        ["dispatched", { state: "dispatched", last_error: "connection refused" }],
        ["sent", { state: "sent", last_error: null }],
        ["absent", undefined],
      ])("keeps the ordinary cold-start card for bootstrap=%s", (_label, bootstrap) => {
        coldStartWithBootstrap(bootstrap);

        expect(
          screen.getByText("Putting the finishing touches on your agent..."),
        ).toBeInTheDocument();
        expect(screen.queryByText(/Setup didn't finish/i)).not.toBeInTheDocument();
      });
    });

    it("stays silent when the backend sends no bootstrap block at all", () => {
      useProvMock.mockReturnValue({
        phase: "normal",
        container: { container_status: "running", runtime_phase: "ready", onboarded: true },
        blocked: null,
        refreshInterval: 3000,
        refresh: vi.fn(),
      });
      render(
        <ProvisioningStepper>
          <div>children</div>
        </ProvisioningStepper>,
      );

      expect(screen.getByText("children")).toBeInTheDocument();
      expect(screen.queryByText(/Friday couldn't start/i)).not.toBeInTheDocument();
    });
  });

  // The wait had exactly one bound — a 5-minute soft timeout whose own "Check
  // again" button reset the clock to 0:00 and cleared the banner. A diligent
  // owner therefore deleted their own escalation, and the copy at 5:01 was
  // identical to the copy at hour twenty-one.
  describe("bounding the wait honestly", () => {
    const refreshSpy = vi.fn();
    const renderColdStart = () => {
      gatewayHealthData = { ok: true };
      useProvMock.mockReturnValue({
        phase: "normal",
        container: {
          container_status: "running",
          runtime_phase: "starting",
          container_substatus: "gateway_healthy",
          onboarded: true,
          funnel: { state: "submitted" },
        },
        blocked: null,
        refreshInterval: 3000,
        refresh: refreshSpy,
      });
      return render(
        <ProvisioningStepper>
          <div>children</div>
        </ProvisioningStepper>,
      );
    };

    beforeEach(() => {
      refreshSpy.mockClear();
      vi.useFakeTimers();
    });
    afterEach(() => {
      vi.useRealTimers();
      gatewayHealthData = null;
    });

    it("escalates to a second tier at 15 minutes", () => {
      renderColdStart();

      act(() => void vi.advanceTimersByTime(PROVISION_TIMEOUT_MS + 1_000));
      expect(screen.getByText(/Still going/i)).toBeInTheDocument();
      expect(screen.queryByText(/taking longer than it should/i)).not.toBeInTheDocument();

      act(() => void vi.advanceTimersByTime(PROVISION_STUCK_MS - PROVISION_TIMEOUT_MS));
      expect(screen.getByText(/taking longer than it should/i)).toBeInTheDocument();
      expect(screen.getByText(/something is stuck on our side/i)).toBeInTheDocument();
      expect(screen.queryByText(/Still going/i)).not.toBeInTheDocument();
      // Past the stuck tier the counter stops carrying a promise about how
      // long this normally takes.
      expect(screen.queryByText(/cold starts can take a few minutes/i)).not.toBeInTheDocument();
    });

    it('"Check again" re-polls without erasing the escalation or the clock', () => {
      renderColdStart();
      act(() => void vi.advanceTimersByTime(PROVISION_STUCK_MS + 1_000));
      expect(screen.getByText(/taking longer than it should/i)).toBeInTheDocument();

      fireEvent.click(screen.getByRole("button", { name: /Check again/ }));

      expect(refreshSpy).toHaveBeenCalledTimes(1);
      // It used to setTimedOut(false) + setElapsedMs(0) + restamp the start
      // ref, which is how 21 hours kept reading as five minutes, over and over.
      expect(screen.getByText(/taking longer than it should/i)).toBeInTheDocument();
      expect(screen.queryByText("0:00")).not.toBeInTheDocument();
    });

    it("rolls the counter to h:mm:ss instead of rendering 1260:37", () => {
      renderColdStart();
      act(() => void vi.advanceTimersByTime(3_600_000 + 5_000));
      expect(screen.getByText(/^1:00:0\d$/)).toBeInTheDocument();
    });
  });

  it("does not render the Teams-degraded notice on a healthy ready container", () => {
    useProvMock.mockReturnValue({
      phase: "normal",
      container: { container_status: "running", runtime_phase: "ready", onboarded: true },
      blocked: null,
      refreshInterval: 3000,
      refresh: vi.fn(),
    });

    render(
      <ProvisioningStepper>
        <div>children</div>
      </ProvisioningStepper>,
    );

    expect(screen.getByText("children")).toBeInTheDocument();
    expect(screen.queryByText(/Teams needs attention/i)).not.toBeInTheDocument();
  });
});

describe("ProvisioningStepper per-container onboarding gate", () => {
  // Every test here needs a loaded Clerk user (the funnel branch is a
  // no-op when `user` is null — see the other describe block's default)
  // and a `normal` gate phase so the gate's own preconditions are met.
  // Whether the funnel shows is then decided by `container.onboarded`, the
  // AUTHORITATIVE per-container fact (reset on wipe/reprovision), which rides
  // on the /container/status poll the hook already returns — no separate
  // request. `firstRun.briefSubmitted` is durable Clerk metadata that nothing
  // clears, so it is NOT a gate; it only bridges this session's submit to the
  // next poll (a false->true edge WHILE MOUNTED dismisses the funnel without a
  // flash). A stale `true` already present at mount must NOT suppress the
  // funnel — that was the "returning owner never re-onboards" bug.
  const renderWith = (container: Record<string, unknown>) => {
    useProvMock.mockReturnValue({
      phase: "normal",
      container,
      blocked: null,
      refreshInterval: 3000,
      refresh: vi.fn(),
    });
    return render(
      <ProvisioningStepper>
        <div>children</div>
      </ProvisioningStepper>,
    );
  };

  beforeEach(() => {
    useProvMock.mockReset();
    apiPost.mockClear();
    apiGet.mockClear();
    apiPut.mockClear();
    apiDel.mockClear();
    billingMockIsSubscribed = true;
    billingMockAccount = { provider_choice: "bedrock_claude" };
    billingMockRefresh = vi.fn();
    billingMockStartCheckout.mockClear();
    clerkMockUser = { id: "user_1", unsafeMetadata: {} };
    clerkMockIsLoaded = true;
    clerkMockOrganization = null;
  });

  afterEach(() => {
    apiGet.mockResolvedValue({});
    clerkMockOrganization = null;
  });

  it("shows the funnel when the container is not onboarded and the per-user marker is unset", () => {
    renderWith({ container_status: "running", runtime_phase: "ready", onboarded: false });

    expect(screen.getByText(/get started/i)).toBeInTheDocument();
    expect(screen.queryByText("children")).not.toBeInTheDocument();
  });

  it("shows the funnel when `onboarded` is absent (older backend / new container)", () => {
    // A backend that doesn't send the field, or a brand-new container, must
    // still show the funnel — absence is not "onboarded".
    renderWith({ container_status: "running", runtime_phase: "ready" });

    expect(screen.getByText(/get started/i)).toBeInTheDocument();
    expect(screen.queryByText("children")).not.toBeInTheDocument();
  });

  it("does not show the funnel when the container is already onboarded", () => {
    // The whole point of the per-container flag: a second org member on an
    // already-onboarded container (their own per-user marker still empty)
    // goes straight to the app.
    renderWith({ container_status: "running", runtime_phase: "ready", onboarded: true });

    expect(screen.queryByText(/get started/i)).not.toBeInTheDocument();
    expect(screen.getByText("children")).toBeInTheDocument();
  });

  it("dismisses the funnel once the brief is SUBMITTED, before onboarding completes (Option B: watch Friday work)", () => {
    // The funnel owns the screen only until funnel_state advances off
    // "not_started". A container that is ready + NOT yet onboarded but whose
    // brief has been submitted drops straight into Friday's chat (children) so
    // the user watches the server-dispatched kickoff stream live, rather than
    // holding a "Setting things up…" state until the kickoff completes.
    renderWith({
      container_status: "running",
      runtime_phase: "ready",
      onboarded: false,
      funnel: { state: "submitted" },
    });

    expect(screen.queryByText(/get started/i)).not.toBeInTheDocument();
    expect(screen.getByText("children")).toBeInTheDocument();
  });

  it("shows the funnel when onboarded is false even if a STALE durable briefSubmitted is already true at mount (the returning-owner bug)", () => {
    // The bug this fix targets: `briefSubmitted` is durable Clerk metadata that
    // survives a container wipe/reprovision and nothing ever clears it. A
    // returning owner whose container was reprovisioned has
    // `container.onboarded === false` (fresh, authoritative) but a STALE
    // `briefSubmitted === true`. Reading the stale flag as a gate suppressed
    // the funnel permanently, so a legitimately reprovisioned org never
    // re-onboarded. `container.onboarded` is authoritative — the funnel MUST
    // show regardless of Clerk metadata (spec PR-9, CLIENT-HOOK deleted the
    // same-session flash-bridge entirely, so there is no bridge state to
    // even consider a stale value stale — `onboarded` is the SOLE input).
    clerkMockUser = {
      id: "user_1",
      unsafeMetadata: { firstRun: { briefSubmitted: true } },
    };
    renderWith({ container_status: "running", runtime_phase: "ready", onboarded: false });

    expect(screen.getByText(/get started/i)).toBeInTheDocument();
    expect(screen.queryByText("children")).not.toBeInTheDocument();
  });

  it("keeps the funnel up on a Clerk-metadata-only change — no same-session flash-bridge anymore", () => {
    // spec PR-9, CLIENT-HOOK deleted the `briefBaselineRef`/
    // `sessionBriefSubmittedRef` same-session flash-bridge. The funnel now
    // dismisses SOLELY on the backend's own `container.onboarded` flipping
    // true (on the next shared bounded-loop poll) — a Clerk `firstRun`
    // metadata write alone (with `onboarded` still false on the mocked
    // hook result) must NOT dismiss it.
    clerkMockUser = { id: "user_1", unsafeMetadata: {} };
    const container = { container_status: "running", runtime_phase: "ready", onboarded: false };
    useProvMock.mockReturnValue({
      phase: "normal",
      container,
      blocked: null,
      refreshInterval: 3000,
      refresh: vi.fn(),
    });
    const { rerender } = render(
      <ProvisioningStepper>
        <div>children</div>
      </ProvisioningStepper>,
    );
    // Funnel shows: onboarded false, no submit yet this session.
    expect(screen.getByText(/get started/i)).toBeInTheDocument();

    // Clerk metadata alone flips (briefSubmitted:true) — but the composed
    // contract's `onboarded` is UNCHANGED (still false).
    clerkMockUser = {
      id: "user_1",
      unsafeMetadata: { firstRun: { briefSubmitted: true } },
    };
    rerender(
      <ProvisioningStepper>
        <div>children</div>
      </ProvisioningStepper>,
    );

    // No flash-bridge left to dismiss it early — the funnel stays up until
    // the backend's own `onboarded` flips true.
    expect(screen.getByText(/get started/i)).toBeInTheDocument();
    expect(screen.queryByText("children")).not.toBeInTheDocument();

    // The next shared-loop poll reports onboarded:true — NOW it dismisses.
    useProvMock.mockReturnValue({
      phase: "normal",
      container: { ...container, onboarded: true },
      blocked: null,
      refreshInterval: 3000,
      refresh: vi.fn(),
    });
    rerender(
      <ProvisioningStepper>
        <div>children</div>
      </ProvisioningStepper>,
    );
    expect(screen.queryByText(/get started/i)).not.toBeInTheDocument();
    expect(screen.getByText("children")).toBeInTheDocument();
  });

  it("does not show the funnel when the gate is blocked, even with a loaded user and onboarded false", () => {
    // The `gatePhase === "normal"` guard is load-bearing: a blocked /
    // needs-signup / team-pending state must win over the funnel. Even with
    // the funnel's own preconditions met (user loaded, onboarded false, no
    // this-session submit), a blocked gate shows the blocked card, never the
    // funnel.
    clerkMockUser = { id: "user_1", unsafeMetadata: {} };
    useProvMock.mockReturnValue({
      phase: "blocked",
      container: { container_status: "provisioning", runtime_phase: "provisioning", onboarded: false },
      blocked: {
        code: "credits_required",
        title: "Top up Claude credits to start your container",
        message: "Top up some Claude credits to start your Bedrock container.",
        action: {
          kind: "link",
          label: "Top up now",
          href: "/settings/billing#credits",
          admin_only: false,
        },
        owner_role: "admin",
      },
      refreshInterval: 5000,
      refresh: vi.fn(),
    });

    render(
      <ProvisioningStepper>
        <div>children</div>
      </ProvisioningStepper>,
    );

    // Funnel absent; the blocked card is shown instead.
    expect(screen.queryByText(/get started/i)).not.toBeInTheDocument();
    expect(
      screen.getByText(/Top up Claude credits to start your container/),
    ).toBeInTheDocument();
  });

  it("shows the Setup-failed card (not the funnel) for a terminal error with a loaded, not-onboarded user", () => {
    // Branch-order guard: the funnel sits ABOVE the error card and fires for a
    // loaded, not-onboarded user on a normal gate — but a terminally-failed
    // container must show the Setup-failed card instead (submitting a brief to
    // a dead backend is a dead end). The funnel is guarded on !isTerminalError.
    useProvMock.mockReturnValue({
      phase: "normal",
      container: {
        container_status: "error",
        container_substatus: "provision_failed",
        runtime_phase: "provisioning",
        onboarded: false,
        last_error: "ECS task failed to start",
      },
      blocked: null,
      refreshInterval: 3000,
      refresh: vi.fn(),
    });

    render(
      <ProvisioningStepper>
        <div>children</div>
      </ProvisioningStepper>,
    );

    expect(screen.getByText(/Setup failed/i)).toBeInTheDocument();
    // The funnel must NOT preempt the terminal error card.
    expect(screen.queryByText(/get started/i)).not.toBeInTheDocument();
  });

  it("holds a neutral loader (not children) until Clerk resolves the user on a not-yet-onboarded ready container", () => {
    // The funnel branch requires a loaded user; without the guard a
    // not-onboarded ready container would flash `children` (the app) for a
    // frame before the funnel takes over once `user` loads. isLoaded=false +
    // not onboarded => neutral loader, neither children nor the funnel.
    clerkMockIsLoaded = false;
    clerkMockUser = null;
    const { container } = renderWith({ container_status: "running", runtime_phase: "ready", onboarded: false });

    expect(screen.queryByText("children")).not.toBeInTheDocument();
    expect(screen.queryByText(/get started/i)).not.toBeInTheDocument();
    expect(container.querySelector(".animate-spin")).toBeInTheDocument();
  });

  it("holds a loader (never the dock) for a ready, not-onboarded container while Clerk `user` is null-but-LOADED — the ready-return must not beat the funnel", () => {
    // ROOT CAUSE of "paid org owner never sees the funnel". After an org
    // `setActive` JWT refresh, useUser transiently returns {isLoaded:true,
    // user:null} while the container is already warm-ready. The funnel gate
    // needs a truthy `user`, so it is skipped; the `!userLoaded` loader is ALSO
    // skipped (Clerk reports loaded); and the `phase === "ready"` return would
    // hand straight off to `children` (the dockable workspace), beating the
    // funnel for good — the owner is booted into a contextless workspace and
    // the brief is never submitted. `container.onboarded === false` is
    // authoritative: hold a loader, never the dock, until `user` resolves.
    clerkMockUser = null; // loaded-but-null (isLoaded stays the default true)
    clerkMockIsLoaded = true;
    const { container } = renderWith({ container_status: "running", runtime_phase: "ready", onboarded: false });

    expect(screen.queryByText("children")).not.toBeInTheDocument();
    expect(screen.queryByText(/get started/i)).not.toBeInTheDocument();
    expect(container.querySelector(".animate-spin")).toBeInTheDocument();
  });

  it("flips from the loader to the funnel once `user` resolves after the null-but-loaded window (loader is a bridge, not a dead end)", () => {
    // The hold is temporary: the instant useUser resolves the user object, the
    // funnel takes over the very same ready/not-onboarded container — it is
    // never permanently stuck on the loader, and never leaks the dock.
    clerkMockUser = null;
    clerkMockIsLoaded = true;
    const container = { container_status: "running", runtime_phase: "ready", onboarded: false };
    useProvMock.mockReturnValue({
      phase: "normal",
      container,
      blocked: null,
      refreshInterval: 3000,
      refresh: vi.fn(),
    });
    const { rerender } = render(
      <ProvisioningStepper>
        <div>children</div>
      </ProvisioningStepper>,
    );
    // Null-but-loaded window: neither the funnel nor the dock, just the loader.
    expect(screen.queryByText(/get started/i)).not.toBeInTheDocument();
    expect(screen.queryByText("children")).not.toBeInTheDocument();

    // useUser resolves the user object.
    clerkMockUser = { id: "user_1", unsafeMetadata: {} };
    rerender(
      <ProvisioningStepper>
        <div>children</div>
      </ProvisioningStepper>,
    );

    expect(screen.getByText(/get started/i)).toBeInTheDocument();
    expect(screen.queryByText("children")).not.toBeInTheDocument();
  });

  it("shows the funnel for a SUBSCRIBED (non-trial) ORG owner with a ready, not-onboarded container", () => {
    // The reported scenario: a paid org (subscription active, provider_choice
    // bedrock_claude, NOT trial_local) whose container is ready and never
    // onboarded. The funnel is keyed on container.onboarded, not the plan or
    // tenancy — a loaded org owner must reach it exactly like a personal one.
    clerkMockUser = { id: "user_1", unsafeMetadata: {} };
    clerkMockOrganization = { id: "org_1", name: "Acme" };
    billingMockIsSubscribed = true;
    billingMockAccount = { provider_choice: "bedrock_claude" };
    renderWith({ container_status: "running", runtime_phase: "ready", onboarded: false });

    expect(screen.getByText(/get started/i)).toBeInTheDocument();
    expect(screen.queryByText("children")).not.toBeInTheDocument();
  });

  it("shows the funnel for a trial_local owner with a ready, not-onboarded container (regression: not gated on isTrialLocal)", () => {
    // The free-tier owner must still reach the funnel — the fix keys the funnel
    // on container.onboarded for ALL owner types, so trial_local is neither a
    // requirement (org/paid must reach it) nor a special case here.
    clerkMockUser = { id: "user_1", unsafeMetadata: {} };
    billingMockIsSubscribed = true;
    billingMockAccount = { provider_choice: null, subscription_status: "trial_local" };
    renderWith({ container_status: "running", runtime_phase: "ready", onboarded: false });

    expect(screen.getByText(/get started/i)).toBeInTheDocument();
    expect(screen.queryByText("children")).not.toBeInTheDocument();
  });
});

// The former "cold-start poll re-arming (derived from phase)" and "poll
// watchdog (throttle-killed SWR self-recovery)" describe blocks are DELETED
// here (spec PR-9, CLIENT-HOOK). Both tested mechanisms this stepper used
// to own directly: a consumer-supplied `refreshInterval`/`revalidateOnFocus`
// passed into `useProvisioningState` (derived from the live phase to re-arm
// a 3s cold-start poll and go quiet at ready), and a 1s-interval watchdog
// that fired an out-of-band `refresh()` at most once/10s to route around a
// throttled SWR chain. Both are superseded by the ONE shared bounded loop
// behind `useOnboardingStatus` (visibility-gated cadence: 5s while unsettled
// and within the first 60s, 15s after, 0 once ready; PLUS a raw
// `setInterval` backstop independent of SWR's own scheduling; PLUS a
// fetch-timeout AbortController so a genuinely hung request — the actual
// occlusion-throttling kill vector per the CLIENT-HOOK research — can't
// wedge it either). That cadence/backstop/timeout behavior is covered once,
// at the shared-hook level, in
// packages/shared/src/hooks/useOnboardingStatus.test.ts — not re-tested per
// consumer here.

// Ready re-kick: the observed not-ready→ready transition on the HTTP status
// poll must revive every gateway `rpc|*` SWR key. Those keys go permanently
// dead during a long cold start (fixed 3s error-retry chain capped at 100,
// and SWR never fires refreshInterval while a key holds an error), and
// nothing else observes readiness on a channel independent of the gateway
// WS — a stuck wake used to need a full page reload to recover.
describe("ProvisioningStepper ready re-kick", () => {
  const baseProv = {
    phase: "normal",
    blocked: null,
    refreshInterval: 3000,
    refresh: vi.fn(),
  };

  beforeEach(() => {
    swrGlobalMutate.mockClear();
    clerkMockUser = { id: "user_1", unsafeMetadata: {} };
    clerkMockIsLoaded = true;
    clerkMockOrganization = null;
    billingMockIsSubscribed = true;
    billingMockAccount = { provider_choice: "bedrock_claude" };
  });

  function renderPhases(first: Record<string, unknown>) {
    useProvMock.mockReturnValue({ ...baseProv, container: first });
    return render(
      <ProvisioningStepper>
        <div>children</div>
      </ProvisioningStepper>,
    );
  }

  function rerenderWith(
    rerender: (ui: React.ReactElement) => void,
    container: Record<string, unknown>,
  ) {
    useProvMock.mockReturnValue({ ...baseProv, container });
    rerender(
      <ProvisioningStepper>
        <div>children</div>
      </ProvisioningStepper>,
    );
  }

  it("revives gateway rpc keys on the observed starting→ready transition", () => {
    const { rerender } = renderPhases({
      container_status: "running",
      runtime_phase: "starting",
    });
    expect(swrGlobalMutate).not.toHaveBeenCalled();

    rerenderWith(rerender, {
      container_status: "running",
      runtime_phase: "ready",
      onboarded: true,
    });

    expect(swrGlobalMutate).toHaveBeenCalledTimes(1);
    const filter = swrGlobalMutate.mock.calls[0][0] as (key: unknown) => boolean;
    // Sweeps every gateway RPC key…
    expect(filter("rpc|agents.list|")).toBe(true);
    expect(filter('rpc|health|{"a":1}')).toBe(true);
    // …and nothing else (Teams BFF keys, non-string keys).
    expect(filter("/teams/inbox")).toBe(false);
    expect(filter(["array-key"])).toBe(false);
  });

  it("stays ready without re-sweeping on subsequent polls", () => {
    const { rerender } = renderPhases({
      container_status: "running",
      runtime_phase: "starting",
    });
    rerenderWith(rerender, {
      container_status: "running",
      runtime_phase: "ready",
      onboarded: true,
    });
    rerenderWith(rerender, {
      container_status: "running",
      runtime_phase: "ready",
      onboarded: true,
    });
    expect(swrGlobalMutate).toHaveBeenCalledTimes(1);
  });

  it("does not sweep on a warm mount that first observes ready", () => {
    // A page load straight onto a ready container mount-revalidates every
    // key anyway; the sweep would only double-fetch.
    renderPhases({
      container_status: "running",
      runtime_phase: "ready",
      onboarded: true,
    });
    expect(swrGlobalMutate).not.toHaveBeenCalled();
  });

  it("does not sweep on parked→ready (unpark resumes SWR keys itself)", () => {
    const { rerender } = renderPhases({
      container_status: "running",
      runtime_phase: "ready",
      parked: true,
    });
    rerenderWith(rerender, {
      container_status: "running",
      runtime_phase: "ready",
      onboarded: true,
    });
    expect(swrGlobalMutate).not.toHaveBeenCalled();
  });
});
