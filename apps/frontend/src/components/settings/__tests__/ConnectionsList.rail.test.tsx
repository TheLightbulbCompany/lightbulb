import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { SWRConfig } from "swr";
import React from "react";

// The REAL `useProvisioningRail` — only the API is stubbed, so this covers the
// grouping, the open-workflow filter and the provider superset the sibling
// suite mocks out.
const get = vi.fn();
vi.mock("@/lib/api", () => ({ useApi: () => ({ get, post: vi.fn() }), BACKEND_URL: "" }));

const { notifyApiError, notifyError } = vi.hoisted(() => ({ notifyApiError: vi.fn(), notifyError: vi.fn() }));
vi.mock("@/lib/notify", () => ({ notifyApiError, notifyError }));

vi.mock("next/link", () => ({
  default: ({ href, children }: { href: string; children: React.ReactNode }) => <a href={href}>{children}</a>,
}));

// No OAuth rows at all: every card below comes from the Projects rail.
vi.mock("@/hooks/useConnections", () => ({
  useConnections: () => ({
    connections: [],
    isLoading: false,
    error: null,
    mutate: vi.fn(),
    connectPaste: vi.fn(),
    startOAuth: vi.fn(),
    disconnect: vi.fn(),
  }),
  ApiError: class ApiError extends Error {},
}));

import { ConnectionsList } from "../ConnectionsList";

const RESOURCES = {
  resources: [
    {
      resource_id: "res_1",
      provider: "posthog",
      service: "analytics",
      status: "active",
      resource_name: "Northwind analytics",
      env_keys: ["NEXT_PUBLIC_POSTHOG_PROJECT_TOKEN"],
      level: "company",
    },
    {
      resource_id: "res_2",
      provider: "neon",
      service: "postgres",
      status: "active",
      resource_name: "Primary database",
      env_keys: ["DATABASE_URL"],
      level: "company",
    },
  ],
};

const WORKFLOWS = {
  workflows: [
    { workflow_id: "pwf_1", state: "awaiting_approval", provider: "posthog", service: "svc_analytics" },
    // Settled: neither a chip nor a card of its own.
    { workflow_id: "pwf_2", state: "active", provider: "sentry", service: "svc_errors" },
  ],
};

function renderList() {
  return render(
    <SWRConfig value={{ provider: () => new Map() }}>
      <ConnectionsList canManage />
    </SWRConfig>,
  );
}

beforeEach(() => {
  get.mockReset();
  notifyApiError.mockReset();
  notifyError.mockReset();
});

describe("ConnectionsList — the live provisioning rail", () => {
  it("groups the resources per provider, chips only open work, and cards every rail provider", async () => {
    get.mockImplementation((path: string) =>
      Promise.resolve(path.includes("/workflows") ? WORKFLOWS : RESOURCES),
    );
    renderList();

    // One card per rail provider, even though neither is in the OAuth registry.
    expect(await screen.findByText("Northwind analytics")).toBeInTheDocument();
    expect(screen.getByText("Primary database")).toBeInTheDocument();
    expect(screen.getByText("posthog")).toBeInTheDocument();
    expect(screen.getByText("neon")).toBeInTheDocument();

    // The open workflow chips on its own provider's card; the settled one is
    // neither chipped nor given a card.
    expect(screen.getByText(/Waiting for your approval/)).toBeInTheDocument();
    expect(screen.queryByText("sentry")).toBeNull();
  });

  it("says the read failed rather than showing an empty rail", async () => {
    get.mockRejectedValue(new Error("500"));
    renderList();
    expect(await screen.findByText(/Couldn't load your resources/)).toBeInTheDocument();
  });
});
