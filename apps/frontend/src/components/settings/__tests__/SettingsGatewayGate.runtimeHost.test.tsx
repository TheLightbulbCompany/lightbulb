// @vitest-environment jsdom
/**
 * Review C1. `useRuntimeHost()` only ever resolves if something mounted `useOnboardingStatus`
 * — it is the one caller of `setRuntimeHostScope`/`publishRuntimeHost`. Web had exactly two
 * such mount points and both were under `/chat`, so a HARD load of `/settings?panel=skills`
 * left every panel that holds its RPC on a definite host spinning forever, on ECS as much as
 * AgentCore. A client-side nav in from `/chat` masked it, because that module state survives
 * the transition.
 *
 * This renders the real gate over the real hook chain (Clerk + fetch mocked, the way
 * `useProvisioningState.test.tsx` does it) and asserts the host actually arrives. The two
 * cases deliberately use different org ids: the published host is scoped, so a second org
 * cannot read the first one's answer, and no cross-test reset is needed.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { SWRConfig } from "swr";
import React from "react";
import { useRuntimeHost } from "@lightbulb/shared";

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

function HostProbe() {
  const host = useRuntimeHost();
  return <div data-testid="host">{host ?? "unknown"}</div>;
}

function statusBody(runtimeHost: string) {
  return new Response(
    JSON.stringify({
      container_status: "running",
      container_substatus: "gateway_healthy",
      service_name: "x",
      runtime_phase: "ready",
      lifecycle: "ready",
      chat_available: true,
      runtime_host: runtimeHost,
    }),
    { status: 200, headers: { "Content-Type": "application/json" } },
  );
}

describe("a hard load of /settings resolves the runtime host", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    auth.isSignedIn = true;
  });
  afterEach(() => vi.restoreAllMocks());

  it.each([
    ["ecs", "org_ecs"],
    ["agentcore", "org_agentcore"],
  ])("publishes %s with nothing from /chat mounted", async (host, orgId) => {
    auth.orgId = orgId;
    auth.uiOrgId = orgId;
    auth.getToken.mockResolvedValue(token(orgId));
    global.fetch = vi.fn().mockResolvedValue(statusBody(host));

    render(
      <SWRConfig value={{ provider: () => new Map() }}>
        <SettingsGatewayGate>
          <HostProbe />
        </SettingsGatewayGate>
      </SWRConfig>,
    );

    // Before the fix this stayed "unknown" forever — no error, no retry, no timeout.
    await waitFor(() => expect(screen.getByTestId("host")).toHaveTextContent(host));
  });
});
