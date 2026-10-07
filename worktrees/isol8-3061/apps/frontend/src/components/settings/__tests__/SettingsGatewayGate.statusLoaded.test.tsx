// @vitest-environment jsdom
/**
 * Review C1. `useStatusLoaded()` only ever resolves if something mounted `useOnboardingStatus`
 * — it is the one caller of `setStatusScope`/`publishStatusLoaded`. Web had exactly two
 * such mount points and both were under `/chat`, so a HARD load of `/settings?panel=skills`
 * left every panel that holds its RPC on a loaded status spinning forever. A client-side nav in from `/chat` masked it, because that module state survives
 * the transition.
 *
 * This renders the real gate over the real hook chain (Clerk + fetch mocked, the way
 * `useProvisioningState.test.tsx` does it) and asserts the signal actually arrives. The two
 * cases deliberately use different org ids: the published signal is scoped, so a second org
 * cannot read the first one's answer, and no cross-test reset is needed.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { SWRConfig } from "swr";
import React from "react";
import { useStatusLoaded } from "@lightbulb/shared";

import { SettingsGatewayGate } from "../SettingsGatewayGate";

const auth = vi.hoisted(() => ({
  isSignedIn: true,
  orgId: "org_123" as string | undefined,
  uiOrgId: "org_123" as string | undefined,
  userId: "user_1",
  sessionId: "sess_1",
  getToken: vi.fn(),
}));
const token = (orgId: string) =>
  `header.${btoa(JSON.stringify({ o: { id: orgId } }))}.signature`;

vi.mock("@clerk/nextjs", () => ({
  useAuth: () => auth,
  useOrganization: () => ({
    isLoaded: true,
    organization: auth.uiOrgId ? { id: auth.uiOrgId } : null,
  }),
}));

// The live socket is irrelevant here and would need its own token plumbing.
vi.mock("@/hooks/useLiveEvents", () => ({
  LiveEventsProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));

function LoadedProbe() {
  return <div data-testid="loaded">{useStatusLoaded() ? "loaded" : "unknown"}</div>;
}

function statusBody() {
  return new Response(
    JSON.stringify({
      container_status: "running",
      container_substatus: "gateway_healthy",
      service_name: "x",
      runtime_phase: "ready",
      lifecycle: "ready",
      chat_available: true,
    }),
    { status: 200, headers: { "Content-Type": "application/json" } },
  );
}

describe("a hard load of /settings resolves the status signal", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    auth.isSignedIn = true;
  });
  afterEach(() => vi.restoreAllMocks());

  it("publishes the signal with nothing from /chat mounted", async () => {
    const orgId = "org_loaded";
    auth.orgId = orgId;
    auth.uiOrgId = orgId;
    auth.getToken.mockResolvedValue(token(orgId));
    global.fetch = vi.fn().mockResolvedValue(statusBody());

    render(
      <SWRConfig value={{ provider: () => new Map() }}>
        <SettingsGatewayGate>
          <LoadedProbe />
        </SettingsGatewayGate>
      </SWRConfig>,
    );

    // Before the fix this stayed "unknown" forever — no error, no retry, no timeout.
    await waitFor(() => expect(screen.getByTestId("loaded")).toHaveTextContent("loaded"));
  });

  /**
   * Review N1. This reader wants to KNOW the runtime, not to be a reason to spend on it. The
   * status read is cold now and the `wake` param is gone, so it is a plain GET.
   */
  it("reads the status with a plain GET and no wake param", async () => {
    auth.orgId = "org_nowake";
    auth.uiOrgId = "org_nowake";
    auth.getToken.mockResolvedValue(token("org_nowake"));
    const fetchMock = vi.fn().mockResolvedValue(statusBody());
    global.fetch = fetchMock;

    render(
      <SWRConfig value={{ provider: () => new Map() }}>
        <SettingsGatewayGate>
          <LoadedProbe />
        </SettingsGatewayGate>
      </SWRConfig>,
    );

    await waitFor(() => expect(screen.getByTestId("loaded")).toHaveTextContent("loaded"));
    const urls = fetchMock.mock.calls.map(([url]) => String(url));
    expect(urls.length).toBeGreaterThan(0);
    expect(urls.some((url) => url.includes("wake="))).toBe(false);
  });
});
