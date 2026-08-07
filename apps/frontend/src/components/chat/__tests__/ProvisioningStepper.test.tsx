import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, act } from "@testing-library/react";
import React from "react";

// Gateway event bus stub — lets tests push status_change events the way
// the backend's connection pool does over the WebSocket.
let gwEventHandlers: Array<(event: string, payload: unknown) => void> = [];
function emitGatewayEvent(event: string, payload: unknown) {
  for (const h of [...gwEventHandlers]) h(event, payload);
}
vi.mock("@/hooks/useGateway", () => ({
  useGateway: () => ({
    isConnected: true,
    onEvent: (h: (event: string, payload: unknown) => void) => {
      gwEventHandlers.push(h);
      return () => {
        gwEventHandlers = gwEventHandlers.filter((x) => x !== h);
      };
    },
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
  seat_count?: number | null;
  team_seat_price_usd?: number | null;
} | null = {
  provider_choice: "bedrock_claude",
};
// Spy for the /billing/account re-fetch the stepper fires when a container is
// provisioning but billing hasn't caught up to the just-created trial_local.
let billingMockRefresh = vi.fn();
// Spy for the Teams-billing checkout CTA (team_billing_required blocked test).
const billingMockStartTeamCheckout = vi.fn();
vi.mock("@/hooks/useBilling", () => ({
  // Default: provider_choice set, an onboarded owner (the cold-start tests
  // need to get PAST the ProviderPicker gate, which requires isSubscribed AND
  // a persisted provider choice).
  useBilling: () => ({
    isLoading: false,
    isSubscribed: billingMockIsSubscribed,
    account: billingMockAccount,
    refresh: billingMockRefresh,
    startTeamCheckout: billingMockStartTeamCheckout,
  }),
}));

// Configurable so cold-start tests can flip the gateway `health` RPC
// between unanswered (null) and answered.
let gatewayHealthData: Record<string, unknown> | null = null;
vi.mock("@/hooks/useGatewayRpc", () => ({
  useGatewayRpc: () => ({ data: gatewayHealthData, error: null }),
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
    billingMockStartTeamCheckout.mockClear();
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

  it("revalidates container status immediately on a gateway status_change HEALTHY push", () => {
    // The backend pushes status_change("HEALTHY") at the exact moment the
    // pool's container handshake completes. Consuming it kills the 0-3s
    // stepper lag after the container is actually ready.
    const refresh = vi.fn();
    useProvMock.mockReturnValue({
      phase: "normal",
      container: { container_status: "running", runtime_phase: "starting" },
      blocked: null,
      refreshInterval: 3000,
      refresh,
    });

    render(
      <ProvisioningStepper>
        <div>children</div>
      </ProvisioningStepper>,
    );

    expect(refresh).not.toHaveBeenCalled();
    act(() => emitGatewayEvent("status_change", { state: "HEALTHY", reason: "Gateway connected" }));
    expect(refresh).toHaveBeenCalledTimes(1);

    // Unrelated events must not trigger refreshes.
    act(() => emitGatewayEvent("status_change", { state: "GATEWAY_DOWN_DEAD", reason: "lost" }));
    act(() => emitGatewayEvent("heartbeat", {}));
    expect(refresh).toHaveBeenCalledTimes(1);
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
    billingMockAccount = { provider_choice: null, seat_count: 4, team_seat_price_usd: 20 };
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

    // The per-seat card, not the generic gate copy.
    expect(screen.getByText(/4 seats/i)).toBeInTheDocument();
    expect(screen.queryByText(/Subscribe to start your team's containers/)).not.toBeInTheDocument();

    await act(async () => {
      screen.getByRole("button", { name: /subscribe your team/i }).click();
    });
    expect(billingMockStartTeamCheckout).toHaveBeenCalled();

    // The card must carry its own chrome: `.settings-*` classes live in a
    // route-scoped stylesheet /chat never loads, so relying on them here is
    // what made this gate paint as raw unstyled text in prod.
    const card = screen.getByTestId("team-billing-card");
    expect(card.className).not.toMatch(/settings-/);
    expect(card.className).toMatch(/\bbg-white\b/);
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
      expect(
        screen.getByText(/Pick how you want to power your agents/),
      ).toBeInTheDocument();

      // And emphatically NOT the send-them-to-settings card.
      expect(
        screen.queryByRole("link", { name: /Upgrade/ }),
      ).not.toBeInTheDocument();
    },
  );

  it("hides the team CTA for a member when the gate says the action is admin_only", () => {
    billingMockAccount = { provider_choice: null, seat_count: 4, team_seat_price_usd: 20 };
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

    expect(screen.queryByRole("button", { name: /subscribe your team/i })).toBeNull();
    expect(screen.getByText(/ask your org admin/i)).toBeInTheDocument();
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
    expect(
      screen.getByText(/ways to power it/i),
    ).toBeInTheDocument();
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
    expect(screen.queryByText(/ways to power it/i)).not.toBeInTheDocument();
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
    expect(screen.queryByText(/ways to power it/i)).not.toBeInTheDocument();
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
    billingMockStartTeamCheckout.mockClear();
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
