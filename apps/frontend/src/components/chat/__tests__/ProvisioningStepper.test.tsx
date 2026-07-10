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

vi.mock("@clerk/nextjs", () => ({
  useOrganization: () => ({ organization: null, membership: null, isLoaded: true }),
  // Components in the stepper tree (useApi consumers) read useAuth/useUser.
  useAuth: () => ({
    getToken: vi.fn().mockResolvedValue("test-token"),
    isSignedIn: true,
  }),
  useUser: () => ({ user: null, isLoaded: true }),
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
let billingMockAccount: { provider_choice: string | null; subscription_status?: string | null } | null = {
  provider_choice: "bedrock_claude",
};
vi.mock("@/hooks/useBilling", () => ({
  // Default: provider_choice set, an onboarded owner (the cold-start tests
  // need to get PAST the ProviderPicker gate, which requires isSubscribed AND
  // a persisted provider choice).
  useBilling: () => ({
    isLoading: false,
    isSubscribed: billingMockIsSubscribed,
    account: billingMockAccount,
  }),
}));

// Configurable so cold-start tests can flip the gateway `health` RPC
// between unanswered (null) and answered.
let gatewayHealthData: Record<string, unknown> | null = null;
vi.mock("@/hooks/useGatewayRpc", () => ({
  useGatewayRpc: () => ({ data: gatewayHealthData, error: null }),
}));

vi.mock("swr", () => ({
  default: () => ({ data: null, error: null, mutate: vi.fn() }),
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
});
