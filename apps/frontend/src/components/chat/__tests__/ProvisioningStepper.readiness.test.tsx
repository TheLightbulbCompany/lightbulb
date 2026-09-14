// End-to-end readiness: the REAL `useProvisioningState` -> `useOnboardingStatus`
// -> SWR chain over a mocked `fetch`, so the two paths from issue #1878 are
// exercised as the browser runs them rather than through a stubbed hook.
//
//   (a) the page loads mid-boot and the container becomes ready afterwards
//   (b) the page loads when the container is ALREADY ready
//
// Both must end on the workspace, and (a) must get there on the poll's own
// cadence — the stepper's 1s elapsed tick re-renders this tree the whole time,
// which is exactly what used to starve that poll.

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { act, render, screen, waitFor } from "@testing-library/react";
import React from "react";
import { SWRConfig } from "swr";

vi.mock("@clerk/nextjs", () => ({
  useOrganization: () => ({ organization: { id: "org_1" }, membership: { role: "org:admin" }, isLoaded: true }),
  useAuth: () => ({ getToken: async () => `header.${btoa(JSON.stringify({ o: { id: "org_1" } }))}.signature`, isSignedIn: true, orgId: "org_1", userId: "u1" }),
  useUser: () => ({ user: { id: "u1", unsafeMetadata: {} }, isLoaded: true }),
}));

vi.mock("@/hooks/useLiveEvents", () => ({
  useLiveEvents: () => ({ isConnected: true, onEvent: () => () => {} }),
}));

// The gateway health RPC answers (the container really is up) — the state that
// resolves the cold-start card to its LAST step, which is where the founder's
// workspace sat.
vi.mock("@/hooks/useRuntimeRpc", () => ({
  useRuntimeRpc: () => ({ data: { ok: true }, error: null }),
}));

vi.mock("@/hooks/useBilling", () => ({
  useBilling: () => ({
    isLoading: false,
    isSubscribed: true,
    account: { provider_choice: "bedrock_claude" },
    refresh: vi.fn(),
    startCheckout: vi.fn(),
  }),
}));

vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));
vi.mock("posthog-js/react", () => ({
  usePostHog: () => null,
  PostHogProvider: ({ children }: { children: React.ReactNode }) => children,
}));
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
}));
vi.mock("@/lib/api", () => ({
  useApi: () => ({ get: vi.fn().mockResolvedValue({}), post: vi.fn().mockResolvedValue({}), put: vi.fn(), del: vi.fn() }),
  ApiError: class ApiError extends Error {},
}));

import { ProvisioningStepper } from "../ProvisioningStepper";

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

const READY = {
  lifecycle: "ready",
  runtime_phase: "ready",
  chat_available: true,
  container_status: "running",
  container_substatus: "gateway_healthy",
  parked: false,
  onboarded: true,
  last_error: null,
  last_error_at: null,
  created_at: null,
  updated_at: null,
  region: "us-east-1",
  generations: { provision_generation: "g1", onboarding_generation: "g1" },
  funnel: { state: "complete", goal_id: null, project_id: null, submitted_at: null, completed_at: null },
  bootstrap: { state: "delivered", dispatched_at: null, delivered_at: null, attempts: 0, last_error: null },
  provider: { choice: "bedrock_claude", byo_provider: null, is_org: true },
  teams_state: "ready",
};

// What the backend answers before its own pool self-heal dial completes: the
// task is running, the gateway liveness row isn't there yet.
const STARTING = {
  ...READY,
  lifecycle: "starting",
  runtime_phase: "starting",
  chat_available: false,
  container_substatus: "task_running",
};

let body: unknown = READY;

function renderStepper() {
  return render(
    <SWRConfig value={{ provider: () => new Map() }}>
      <ProvisioningStepper>
        <div>workspace</div>
      </ProvisioningStepper>
    </SWRConfig>,
  );
}

beforeEach(() => {
  body = READY;
  global.fetch = vi.fn().mockImplementation(() =>
    Promise.resolve(
      new Response(JSON.stringify(body), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    ),
  ) as unknown as typeof fetch;
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("ProvisioningStepper readiness (issue #1878)", () => {
  it("(b) renders the workspace when the FIRST status response is already ready", async () => {
    renderStepper();
    await waitFor(() => expect(screen.getByText("workspace")).toBeInTheDocument());
    expect(screen.queryByText(/finishing touches/i)).not.toBeInTheDocument();
  });

  it("(a) shows the workspace mid-boot and drops the agents banner once the container is ready", async () => {
    vi.useFakeTimers();
    body = STARTING;
    renderStepper();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(200);
    });
    // Messaging never needs the container: the workspace renders mid-boot,
    // with the agents banner instead of the setup card.
    expect(screen.getByText("workspace")).toBeInTheDocument();
    expect(screen.getByText(/Your agents are getting ready/i)).toBeInTheDocument();
    expect(screen.queryByText(/finishing touches/i)).not.toBeInTheDocument();

    // The container finishes booting. Nothing else happens: no click, no
    // reload, no websocket push — only the status poll, whose cadence is 5s
    // while the elapsed timer re-renders this tree every second.
    body = READY;
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });

    await vi.waitFor(() => expect(screen.queryByText(/Your agents are getting ready/i)).not.toBeInTheDocument());
    expect(screen.getByText("workspace")).toBeInTheDocument();
  });
});
