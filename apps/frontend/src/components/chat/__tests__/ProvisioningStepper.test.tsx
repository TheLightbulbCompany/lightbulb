import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import React from "react";

// The blocked-state branch in ProvisioningStepper renders before any of the
// container/billing/orchestration logic, so we only need the hooks the
// component touches *up to* that early-return to be functional. Everything
// else gets a no-op stub.

vi.mock("@/hooks/useProvisioningState", () => ({
  useProvisioningState: vi.fn(),
}));

vi.mock("@clerk/nextjs", () => ({
  useOrganization: () => ({ organization: null, membership: null, isLoaded: true }),
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
  useSearchParams: () => new URLSearchParams(),
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
}));

vi.mock("@/hooks/useBilling", () => ({
  useBilling: () => ({ isLoading: false, isSubscribed: true }),
}));

vi.mock("@/hooks/useContainerStatus", () => ({
  useContainerStatus: () => ({ container: null, refresh: vi.fn() }),
}));

vi.mock("@/hooks/useGatewayRpc", () => ({
  useGatewayRpc: () => ({ data: null, error: null }),
}));

vi.mock("swr", () => ({
  default: () => ({ data: null, error: null, mutate: vi.fn() }),
}));

vi.mock("@/lib/analytics", () => ({
  capture: vi.fn(),
}));

import { useProvisioningState } from "@/hooks/useProvisioningState";
import { ProvisioningStepper } from "../ProvisioningStepper";

const useProvMock = useProvisioningState as ReturnType<typeof vi.fn>;

describe("ProvisioningStepper blocked-state rendering", () => {
  beforeEach(() => {
    useProvMock.mockReset();
    apiPost.mockClear();
    apiGet.mockClear();
    apiPut.mockClear();
    apiDel.mockClear();
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
});
