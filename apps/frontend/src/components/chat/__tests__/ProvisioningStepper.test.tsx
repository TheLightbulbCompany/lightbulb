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
vi.mock("@clerk/nextjs", () => ({
  useOrganization: () => ({ organization: null, membership: null, isLoaded: true }),
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
      container: { status: "provisioning", phase: "provisioning", substatus: "task_running" },
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
      container: { status: "provisioning", phase: "provisioning", substatus: "service_created" },
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
      container: { status: "running", phase: "starting", substatus: "gateway_healthy" },
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
      container: { status: "running", phase: "starting", substatus: "gateway_healthy" },
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
      container: { status: "running", phase: "starting" },
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
      container: { status: "provisioning", phase: "provisioning" },
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
      container: { status: "provisioning", phase: "provisioning", substatus: "service_created" },
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
        status: "error",
        substatus: "provision_failed",
        phase: "provisioning",
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

  it("keeps the provisioning stepper (NOT the Setup-failed card) for a RETRYING error", () => {
    // status="error" + substatus="error_retrying" is transient — the backend is
    // auto-retrying. The stepper must keep showing progress with a calm
    // "retrying" line; the terminal "Setup failed" card must NOT appear. This
    // is the whole point of the split: a dead container fails loudly, a
    // retrying one keeps showing progress.
    useProvMock.mockReturnValue({
      phase: "normal",
      container: {
        status: "error",
        substatus: "error_retrying",
        phase: "provisioning",
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

  it("renders the ready app plus a non-blocking Teams-degraded notice when teams_status=failed", () => {
    // teams_status is a TOP-LEVEL, non-blocking signal: Teams seeding failed but
    // the container is healthy and chat works. The app (children) must render,
    // with a small dismissible "Teams needs attention" affordance over it —
    // never a blocking interstitial.
    useProvMock.mockReturnValue({
      phase: "normal",
      container: { status: "running", phase: "ready", onboarded: true, teams_status: "failed" },
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
      container: { status: "running", phase: "ready", onboarded: true },
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
  });

  afterEach(() => {
    apiGet.mockResolvedValue({});
  });

  it("shows the funnel when the container is not onboarded and the per-user marker is unset", () => {
    renderWith({ status: "running", phase: "ready", onboarded: false });

    expect(screen.getByText(/get started/i)).toBeInTheDocument();
    expect(screen.queryByText("children")).not.toBeInTheDocument();
  });

  it("shows the funnel when `onboarded` is absent (older backend / new container)", () => {
    // A backend that doesn't send the field, or a brand-new container, must
    // still show the funnel — absence is not "onboarded".
    renderWith({ status: "running", phase: "ready" });

    expect(screen.getByText(/get started/i)).toBeInTheDocument();
    expect(screen.queryByText("children")).not.toBeInTheDocument();
  });

  it("does not show the funnel when the container is already onboarded", () => {
    // The whole point of the per-container flag: a second org member on an
    // already-onboarded container (their own per-user marker still empty)
    // goes straight to the app.
    renderWith({ status: "running", phase: "ready", onboarded: true });

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
    // show. (A `true` present at first mount is treated as stale: the baseline
    // captures it and never arms the same-session latch.)
    clerkMockUser = {
      id: "user_1",
      unsafeMetadata: { firstRun: { briefSubmitted: true } },
    };
    renderWith({ status: "running", phase: "ready", onboarded: false });

    expect(screen.getByText(/get started/i)).toBeInTheDocument();
    expect(screen.queryByText("children")).not.toBeInTheDocument();
  });

  it("suppresses the funnel after THIS session's submit (no flash) even before the poll flips onboarded", () => {
    // Same-session flash-bridge. The funnel is mounted with briefSubmitted
    // unset, so it shows. Then the user submits: FirstRunFunnel's
    // writeFirstRun sets briefSubmitted:true in Clerk metadata, re-rendering
    // useUser. That false->true edge WHILE MOUNTED arms the in-memory latch,
    // dismissing the funnel immediately instead of leaving it up until the
    // next /container/status poll flips onboarded:true — while onboarded is
    // still false. This is the ONLY thing briefSubmitted is allowed to do
    // (contrast the stale-at-mount test above).
    clerkMockUser = { id: "user_1", unsafeMetadata: {} };
    const container = { status: "running", phase: "ready", onboarded: false };
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

    // This session's submit lands: briefSubmitted flips false -> true.
    clerkMockUser = {
      id: "user_1",
      unsafeMetadata: { firstRun: { briefSubmitted: true } },
    };
    rerender(
      <ProvisioningStepper>
        <div>children</div>
      </ProvisioningStepper>,
    );

    // Latch armed -> funnel dismissed at once, even though onboarded is still false.
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
      container: { status: "provisioning", phase: "provisioning", onboarded: false },
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
        status: "error",
        substatus: "provision_failed",
        phase: "provisioning",
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
    const { container } = renderWith({ status: "running", phase: "ready", onboarded: false });

    expect(screen.queryByText("children")).not.toBeInTheDocument();
    expect(screen.queryByText(/get started/i)).not.toBeInTheDocument();
    expect(container.querySelector(".animate-spin")).toBeInTheDocument();
  });
});
