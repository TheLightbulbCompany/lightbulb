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

const mockSuperAgent = vi.hoisted(() => ({ on: false }));
vi.mock("@/hooks/useLiveEvents", () => ({
  useSuperAgent: () => mockSuperAgent.on,
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
    isLoaded: clerkMockIsLoaded,
    isSignedIn: true,
    userId: "user-test",
    orgId: clerkMockOrganization?.id ?? "org-test",
    orgRole: "org:admin",
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

// Configurable so workspace trial_local tests can flip isSubscribed / account
// shape without a persisted provider_choice.
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
import { capture } from "@/lib/analytics";
import { ProvisioningStepper, agentsBannerProgress } from "../ProvisioningStepper";

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
    clerkMockOrganization = null;
  });

  afterEach(() => {
    // beforeEach's mockClear resets call history but not the implementation, so
    // a test that overrides apiGet's resolved value would leak it into later
    // tests if it threw before restoring. Reset the default here unconditionally.
    apiGet.mockResolvedValue({});
  });

  it("keeps the workspace usable through every cold-start phase, with an agents banner instead of the setup card", () => {
    // Messaging never needs the container, so no container state may put the
    // full-screen setup card in front of conversations.
    const base = { phase: "normal", blocked: null, refreshInterval: 3000, refresh: vi.fn() };
    const coldStarts = [
      { container_status: "provisioning", runtime_phase: "provisioning", container_substatus: "service_created" },
      { container_status: "provisioning", runtime_phase: "provisioning", container_substatus: "task_running" },
      { container_status: "running", runtime_phase: "starting", container_substatus: "gateway_healthy" },
    ];
    for (const health of [null, { ok: true }]) {
      gatewayHealthData = health;
      for (const container of coldStarts) {
        useProvMock.mockReturnValue({ ...base, container });
        const { unmount } = render(
          <ProvisioningStepper>
            <div>children</div>
          </ProvisioningStepper>,
        );
        expect(screen.getByText("children")).toBeInTheDocument();
        expect(screen.getByText(/ready in about a minute/i)).toBeInTheDocument();
        // The step label now also appears in the BANNER, so "is the card
        // gone?" can no longer be asked of a step label. Ask it of something
        // only the full-screen card renders: its rotating-ideas eyebrow.
        expect(screen.queryByText(/try asking your agent to/i)).not.toBeInTheDocument();
        unmount();
      }
    }
    gatewayHealthData = null;
  });

  it("super-agent on, real setup in progress (a container row exists): the welcome copy, not the old 'getting ready' one", () => {
    mockSuperAgent.on = true;
    try {
      useProvMock.mockReturnValue({ phase: "normal", blocked: null, refreshInterval: 3000, refresh: vi.fn(),
        container: { container_status: "provisioning", runtime_phase: "provisioning", container_substatus: "task_running" } });
      render(<ProvisioningStepper><div>children</div></ProvisioningStepper>);
      expect(screen.getByText("children")).toBeInTheDocument();
      expect(screen.getByRole("status")).toHaveTextContent("You can write to Lightbulb now — it answers as soon as setup finishes.");
      expect(screen.queryByText(/ready in about a minute/i)).toBeNull();
    } finally { mockSuperAgent.on = false; }
  });

  it("keeps the setup card only until the first status response arrives", () => {
    useProvMock.mockReturnValue({ phase: "normal", blocked: null, refreshInterval: 3000, refresh: vi.fn(), container: null });
    render(
      <ProvisioningStepper>
        <div>children</div>
      </ProvisioningStepper>,
    );
    expect(screen.getByText("Setting up your workspace…")).toBeInTheDocument();
    expect(screen.queryByText("children")).not.toBeInTheDocument();
  });

  it("super-agent on: no setup card at all; the chat is open with a banner saying Lightbulb answers once setup finishes", () => {
    mockSuperAgent.on = true;
    try {
      useProvMock.mockReturnValue({ phase: "normal", blocked: null, refreshInterval: 3000, refresh: vi.fn(), container: null });
      render(
        <ProvisioningStepper>
          <div>children</div>
        </ProvisioningStepper>,
      );
      expect(screen.getByText("children")).toBeInTheDocument();
      expect(screen.queryByText("Setting up your workspace…", { selector: "h1,h2,h3" })).toBeNull();
      expect(screen.getByRole("status")).toHaveTextContent("You can write to Lightbulb now — it answers as soon as setup finishes.");
    } finally { mockSuperAgent.on = false; }
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

  it("renders TeamBillingCard for an org-admin trial_local conversion", async () => {
    clerkMockOrganization = { id: "org_1", name: "Acme" };
    billingMockAccount = {
      provider_choice: null,
      subscription_status: "trial_local",
      workspace_price_usd: 50,
    };
    useProvMock.mockReturnValue({
      phase: "blocked",
      container: null,
      blocked: {
        code: "team_billing_required",
        title: "Subscribe to start your team's containers",
        message: "Your workspace needs an active subscription.",
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
    expect(screen.getByText(/up to 5 people/i)).toBeInTheDocument();
    expect(screen.getByText(/separate ChatGPT subscription/i)).toBeInTheDocument();
    expect(screen.getByText(/provider bills inference separately/i)).toBeInTheDocument();
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

  it("hides the team CTA for a member when the gate says the action is admin_only", () => {
    billingMockAccount = { provider_choice: null, workspace_price_usd: 50 };
    useProvMock.mockReturnValue({
      phase: "blocked",
      container: null,
      blocked: {
        code: "team_billing_required",
        title: "Set up workspace billing",
        message: "Your workspace needs an active Stripe subscription to start.",
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

  it("skips ProviderPicker for a trial_local workspace and proceeds into provisioning", () => {
    // A trial_local workspace has no provider choice. The naive
    // `isSubscribed && provider_choice` ownerOnboarded check would wrongly
    // fall through to the ProviderPicker mid-provision — the trap this test
    // guards against.
    billingMockIsSubscribed = true;
    billingMockAccount = { provider_choice: null, subscription_status: "trial_local" };
    clerkMockOrganization = { id: "org_1", name: "Acme" };

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
    // Must proceed past "payment" into the workspace while the container boots.
    expect(screen.getByText("children")).toBeInTheDocument();
    expect(screen.getByText(/ready in about a minute/i)).toBeInTheDocument();
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

  // Honest contract: a real provisioning failure arrives as status="error".
  // Terminal is any status="error" — provision_failed,
  // the rollout circuit breaker's deployment_failed, or a bare error with no
  // substatus — keyed off container_status, never
  // `phase`. It must say so with Retry + Contact support, never a plain
  // spinner; and since messaging doesn't need the container, it says so in a
  // banner over the working app rather than a card in front of it.
  it.each(["provision_failed", "deployment_failed", null])(
    "shows Setup failed with Retry over the working app for a TERMINAL error (substatus=%s)",
    async (substatus) => {
      const refresh = vi.fn();
      useProvMock.mockReturnValue({
        phase: "normal",
        container: {
          container_status: "error",
          container_substatus: substatus,
          runtime_phase: "provisioning",
          last_error: "ECS task failed to start",
        },
        blocked: null,
        refreshInterval: 3000,
        refresh,
      });

      render(
        <ProvisioningStepper>
          <div>children</div>
        </ProvisioningStepper>,
      );

      expect(screen.getByText("children")).toBeInTheDocument();
      expect(screen.getByText(/Setup failed/i)).toBeInTheDocument();
      expect(screen.getByRole("link", { name: /Contact support/i })).toBeInTheDocument();
      // A deterministic failure is no longer auto-redispatched by the
      // /container/status poll, so Retry must drive the same /container/recover
      // call HealthIndicator's CONTAINER_DOWN action uses -- a bare re-poll
      // would be a dead button.
      await act(async () => {
        fireEvent.click(screen.getByRole("button", { name: /Retry/i }));
      });
      expect(apiPost).toHaveBeenCalledWith("/container/recover", {});
      expect(refresh).toHaveBeenCalledTimes(1);
    },
  );

  it("shows an inline error and stays retryable when /container/recover fails", async () => {
    const refresh = vi.fn();
    apiPost.mockRejectedValueOnce(new Error("network error"));
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
      refresh,
    });

    render(
      <ProvisioningStepper>
        <div>children</div>
      </ProvisioningStepper>,
    );

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /Retry/i }));
    });

    expect(apiPost).toHaveBeenCalledWith("/container/recover", {});
    expect(refresh).not.toHaveBeenCalled();
    expect(screen.getByText(/Retry failed/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Retry/i })).toBeInTheDocument();
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

  // The wait had exactly one bound — a 5-minute soft timeout whose own "Check
  // again" button reset the clock to 0:00 and cleared the banner. A diligent
  // owner therefore deleted their own escalation, and the copy at 5:01 was
  // identical to the copy at hour twenty-one.
  describe("bounding the wait honestly", () => {
    const refreshSpy = vi.fn();
    const renderColdStart = (withContainer = true) => {
      gatewayHealthData = { ok: true };
      useProvMock.mockReturnValue({
        phase: "normal",
        container: withContainer
          ? {
              container_status: "running",
              runtime_phase: "starting",
              container_substatus: "gateway_healthy",
              onboarded: true,
              funnel: { state: "submitted" },
            }
          : null,
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

    it("escalates the agents banner to a second tier at 15 minutes, over the working app", () => {
      renderColdStart();

      act(() => void vi.advanceTimersByTime(PROVISION_TIMEOUT_MS + 1_000));
      expect(screen.getByText(/Still going/i)).toBeInTheDocument();
      expect(screen.queryByText(/taking longer than it should/i)).not.toBeInTheDocument();

      act(() => void vi.advanceTimersByTime(PROVISION_STUCK_MS - PROVISION_TIMEOUT_MS));
      expect(screen.getByText(/taking longer than it should/i)).toBeInTheDocument();
      expect(screen.getByText(/something is stuck on our side/i)).toBeInTheDocument();
      expect(screen.getByRole("link", { name: /Contact support/i })).toBeInTheDocument();
      expect(screen.queryByText(/Still going/i)).not.toBeInTheDocument();
      expect(screen.getByText("children")).toBeInTheDocument();
    });

    it('"Check again" re-polls without erasing the escalation', () => {
      renderColdStart();
      act(() => void vi.advanceTimersByTime(PROVISION_STUCK_MS + 1_000));
      expect(screen.getByText(/taking longer than it should/i)).toBeInTheDocument();

      fireEvent.click(screen.getByRole("button", { name: /Check again/ }));

      expect(refreshSpy).toHaveBeenCalledTimes(1);
      // It used to setTimedOut(false) + setElapsedMs(0) + restamp the start
      // ref, which is how 21 hours kept reading as five minutes, over and over.
      expect(screen.getByText(/taking longer than it should/i)).toBeInTheDocument();
    });

    it('"Check again" on a workspace with NO container row starts it through /container/recover', async () => {
      // Its first provision never started (organization.created gave up) and nothing
      // re-drives it, so a plain re-read would be a dead button.
      useProvMock.mockReturnValue({
        phase: "normal",
        container: { container_status: null, runtime_phase: "provisioning", lifecycle: "provisioning" },
        blocked: null,
        refresh: refreshSpy,
      });
      render(
        <ProvisioningStepper>
          <div>children</div>
        </ProvisioningStepper>,
      );
      // Offered at once: nothing is provisioning, so waiting out the 5-minute slow threshold
      // would leave "ready in about a minute" on screen with nothing running.
      expect(screen.getByText(/hasn't started setting up yet/i)).toBeInTheDocument();

      await act(async () => {
        fireEvent.click(screen.getByRole("button", { name: /Check again/ }));
      });
      expect(apiPost).toHaveBeenCalledWith("/container/recover", {});
    });

    it("rolls the setup card's counter to h:mm:ss instead of rendering 1260:37", () => {
      // The card (and its counter) only remains while no status has arrived.
      renderColdStart(false);
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

// Once a container exists the stepper hands over to the banner, and that banner
// used to be one flat sentence for what can be a 5+ minute wait (upstream's
// sidecars.channels). It now carries the live step — but only while claiming
// progress is still honest.
describe("agents banner progress", () => {
  const baseProv = { phase: "normal", blocked: null, refreshInterval: 3000, refresh: vi.fn() };

  beforeEach(() => {
    useProvMock.mockReset();
    clerkMockUser = null;
    clerkMockIsLoaded = true;
    clerkMockOrganization = null;
    billingMockIsSubscribed = true;
    billingMockAccount = { provider_choice: "bedrock_claude" };
    gatewayHealthData = null;
  });

  afterEach(() => {
    gatewayHealthData = null;
  });

  function renderWithContainer(container: Record<string, unknown>) {
    useProvMock.mockReturnValue({ ...baseProv, container });
    return render(
      <ProvisioningStepper>
        <div>children</div>
      </ProvisioningStepper>,
    );
  }

  it("reports onboarding steps only inside a genuine, settled first-run flow", () => {
    const events = (name: string) => vi.mocked(capture).mock.calls.filter(([event]) => event === name);
    const stepNames = () => events("onboarding_step_entered").map(([, props]) => (props as { step_name: string }).step_name);
    vi.mocked(capture).mockClear();
    const ready = { container_status: "running", runtime_phase: "ready" };
    // A recovery mount (an existing customer's container restarting) reports nothing.
    useProvMock.mockReturnValue({ ...baseProv, container: ready });
    const recovery = render(
      <ProvisioningStepper trigger="recovery">
        <div>children</div>
      </ProvisioningStepper>,
    );
    recovery.unmount();
    // A returning user's real mount: the first frames have no billing answer and
    // no container row, which used to read as "payment" and then "complete" it
    // once the ready state arrived — a phantom traversal on every load.
    billingMockIsSubscribed = false;
    useProvMock.mockReturnValue({ ...baseProv, phase: "loading", container: null });
    const returning = render(
      <ProvisioningStepper>
        <div>children</div>
      </ProvisioningStepper>,
    );
    billingMockIsSubscribed = true;
    useProvMock.mockReturnValue({ ...baseProv, container: ready });
    returning.rerender(
      <ProvisioningStepper>
        <div>children</div>
      </ProvisioningStepper>,
    );
    returning.unmount();
    expect(stepNames()).toEqual([]);
    expect(events("onboarding_step_completed")).toEqual([]);
    // A fresh owner with no container and no plan, once settled, is in the flow.
    billingMockIsSubscribed = false;
    renderWithContainer(null as unknown as Record<string, unknown>);
    expect(stepNames()).toEqual(["payment"]);
  });

  it("places each phase on the same six-step ladder the setup card draws", () => {
    expect(agentsBannerProgress("payment")).toEqual({
      step: 1,
      total: 6,
      activeLabel: "Confirming your plan…",
    });
    expect(agentsBannerProgress("container")).toEqual({
      step: 2,
      total: 6,
      activeLabel: "Setting up your workspace…",
    });
    expect(agentsBannerProgress("gateway")).toEqual({
      step: 3,
      total: 6,
      activeLabel: "Starting your agents…",
    });
    expect(agentsBannerProgress("channels-sidecars")).toEqual({
      step: 4,
      total: 6,
      activeLabel: "Introducing your team…",
    });
    expect(agentsBannerProgress("agent")).toEqual({
      step: 5,
      total: 6,
      activeLabel: "Almost ready…",
    });
    expect(agentsBannerProgress("ready")).toEqual({
      step: 6,
      total: 6,
      activeLabel: "Ready!",
    });
  });

  it("has no position for a phase that isn't on the ladder, rather than inventing one", () => {
    // "provider" is a real machine phase deliberately absent from STEPS_PAID.
    // The banner degrades to its flat copy instead of guessing a number.
    expect(agentsBannerProgress("provider")).toBeNull();
  });

  it.each([
    [
      "container",
      { container_status: "provisioning", runtime_phase: "provisioning", container_substatus: "service_created" },
      null,
      2,
      "Setting up your workspace…",
    ],
    [
      "gateway",
      { container_status: "provisioning", runtime_phase: "provisioning", container_substatus: "task_running" },
      null,
      3,
      "Starting your agents…",
    ],
    [
      "channels-sidecars",
      { container_status: "running", runtime_phase: "starting", container_substatus: "gateway_healthy" },
      null,
      4,
      "Introducing your team…",
    ],
    [
      "agent",
      { container_status: "running", runtime_phase: "starting", container_substatus: "gateway_healthy" },
      { ok: true },
      5,
      "Almost ready…",
    ],
  ])(
    "shows the live step (%s) with a labelled progressbar over the working app",
    (_phase, container, health, step, activeLabel) => {
      gatewayHealthData = health as Record<string, unknown> | null;
      renderWithContainer(container as Record<string, unknown>);

      expect(screen.getByText("children")).toBeInTheDocument();
      expect(screen.getByText(activeLabel as string)).toBeInTheDocument();
      // Plain language only: no infrastructure jargon, no bare step counter.
      expect(screen.queryByText(/container/i)).not.toBeInTheDocument();
      expect(screen.queryByText(/Step \d of 6/)).not.toBeInTheDocument();

      const bar = screen.getByRole("progressbar");
      expect(bar).toHaveAttribute("aria-valuenow", String(step));
      expect(bar).toHaveAttribute("aria-valuemin", "0");
      expect(bar).toHaveAttribute("aria-valuemax", "6");
      expect(bar).toHaveAttribute("aria-valuetext", activeLabel as string);

      // The banner stays the polite live region it was.
      const banner = screen.getByRole("status");
      expect(banner).toHaveAttribute("aria-live", "polite");
      expect(banner).toHaveTextContent(/ready in about a minute/i);
    },
  );

  describe("stops claiming progress once it can't back it up", () => {
    beforeEach(() => {
      vi.useFakeTimers();
    });
    afterEach(() => {
      vi.useRealTimers();
    });

    it("keeps the bar through the first (slow) escalation tier", () => {
      gatewayHealthData = { ok: true };
      renderWithContainer({
        container_status: "running",
        runtime_phase: "starting",
        container_substatus: "gateway_healthy",
      });

      act(() => void vi.advanceTimersByTime(PROVISION_TIMEOUT_MS + 1_000));

      expect(screen.getByText(/Still going/i)).toBeInTheDocument();
      expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "5");
    });

    it("drops the bar entirely when STUCK — a bar next to 'something is stuck' reads as progress", () => {
      gatewayHealthData = { ok: true };
      renderWithContainer({
        container_status: "running",
        runtime_phase: "starting",
        container_substatus: "gateway_healthy",
      });

      act(() => void vi.advanceTimersByTime(PROVISION_STUCK_MS + 1_000));

      expect(screen.getByText(/something is stuck on our side/i)).toBeInTheDocument();
      expect(screen.queryByRole("progressbar")).not.toBeInTheDocument();
      expect(screen.queryByText(/Step \d of 6/)).not.toBeInTheDocument();
      expect(screen.queryByText(/Almost ready/i)).not.toBeInTheDocument();
      // The escalation's own actions survive untouched.
      expect(screen.getByRole("button", { name: /Check again/ })).toBeInTheDocument();
      expect(screen.getByRole("link", { name: /Contact support/i })).toBeInTheDocument();
    });

    it("drops the bar entirely for a TERMINAL failure", () => {
      renderWithContainer({
        container_status: "error",
        container_substatus: "provision_failed",
        runtime_phase: "provisioning",
        last_error: "ECS task failed to start",
      });

      expect(screen.getByText(/Setup failed/i)).toBeInTheDocument();
      expect(screen.queryByRole("progressbar")).not.toBeInTheDocument();
      expect(screen.queryByText(/Step \d of 6/)).not.toBeInTheDocument();
      expect(screen.getByRole("button", { name: /Retry/i })).toBeInTheDocument();
      expect(screen.getByRole("link", { name: /Contact support/i })).toBeInTheDocument();
    });
  });
});

describe("a returning workspace skips the hold (W12-18)", () => {
  const baseProv = { phase: "normal", blocked: null, refreshInterval: 3000, refresh: vi.fn() };
  const mount = () => render(<ProvisioningStepper><div>children</div></ProvisioningStepper>);

  beforeEach(() => {
    localStorage.clear();
    useProvMock.mockReset();
    clerkMockUser = null;
    clerkMockOrganization = null;
    billingMockIsSubscribed = true;
    billingMockAccount = { provider_choice: "bedrock_claude" };
  });

  it("draws the app while the first status read is out once this browser saw it ready, and forgets on a block", () => {
    useProvMock.mockReturnValue({ ...baseProv, phase: "loading", container: null });
    const first = mount();
    expect(screen.getByRole("status")).toHaveTextContent("Getting your workspace ready…");
    first.unmount();

    useProvMock.mockReturnValue({ ...baseProv, container: { container_status: "running", runtime_phase: "ready" } });
    mount().unmount();

    useProvMock.mockReturnValue({ ...baseProv, phase: "loading", container: null });
    const again = mount();
    expect(screen.getByText("children")).toBeInTheDocument();
    expect(screen.queryByText("Getting your workspace ready…")).toBeNull();
    again.unmount();

    useProvMock.mockReturnValue({ ...baseProv, phase: "needs-signup", container: null });
    mount().unmount();
    useProvMock.mockReturnValue({ ...baseProv, phase: "loading", container: null });
    mount();
    expect(screen.getByText("Getting your workspace ready…")).toBeInTheDocument();
  });
});
