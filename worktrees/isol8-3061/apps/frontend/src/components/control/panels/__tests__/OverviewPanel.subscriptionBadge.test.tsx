import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen } from "@testing-library/react";
import React from "react";
import { OverviewPanel } from "../OverviewPanel";

// Covers the "Subscription" InfoRow — a sixth undisclosed raw-enum render
// found via a broader post-review sweep (grep for the `subscription_status`
// field itself, not just the `{subscriptionStatus}` JSX-interpolation shape
// used to find the first four). Unlike ProfilePanel/UsagePanel, this
// InfoRow's value span carries no uppercase/capitalize CSS, so the bug here
// was just the raw enum text itself ("trial_local"), not a transform
// mangling it further — no transform-conditioning needed for this site.
//
// Also covers the `expired` flag: OverviewPanel can be docked as a sibling
// of WorkspaceSidebar in the desktop dockable shell (WorkspaceShell.tsx), so
// an expired free-tier owner must see "Free tier ended" here too — otherwise
// this badge and the sidebar footer badge contradict each other on the same
// screen.

vi.mock("@/hooks/useRuntimeRpc", () => ({
  useRuntimeRpc: (method: string) => {
    if (method === "health") {
      return {
        data: { ok: true, ts: Date.now(), sessions: { count: 0 }, agents: [] },
        error: undefined,
        isLoading: false,
        mutate: vi.fn(),
      };
    }
    return { data: [], error: undefined, isLoading: false, mutate: vi.fn() };
  },
}));

vi.mock("@/hooks/useSystemHealth", () => ({
  useSystemHealth: () => ({
    state: "HEALTHY",
    reason: "ok",
    canRecover: false,
    actionLabel: null,
    recover: vi.fn(),
    isRecovering: false,
  }),
}));

vi.mock("@/hooks/useProvisioningState", () => ({
  useProvisioningState: () => ({
    container: { status: "running", service_name: "svc", region: "us-east-1", created_at: Date.now() },
    isLoading: false,
    error: undefined,
  }),
}));

let billingAccount: { subscription_status: string | null; trial_end?: number | null } | null = {
  subscription_status: "active",
};
vi.mock("@/hooks/useBilling", () => ({
  useBilling: () => ({ account: billingAccount }),
}));

vi.mock("@/lib/api", () => ({
  useApi: () => ({ get: vi.fn().mockResolvedValue({ servers: {} }) }),
}));
vi.mock("swr", () => ({
  default: () => ({ data: undefined, error: undefined, isLoading: false }),
}));

describe("OverviewPanel — Subscription InfoRow", () => {
  const NOW = 1_700_000_000_000;
  beforeEach(() => {
    vi.spyOn(Date, "now").mockReturnValue(NOW);
    billingAccount = { subscription_status: "active" };
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("maps trial_local to Free tier, never the raw enum", () => {
    billingAccount = { subscription_status: "trial_local" };
    render(<OverviewPanel />);
    expect(screen.getByText("Free tier")).toBeInTheDocument();
    expect(screen.queryByText(/trial_local/i)).not.toBeInTheDocument();
  });

  it("capitalizes other statuses via planLabel", () => {
    billingAccount = { subscription_status: "active" };
    render(<OverviewPanel />);
    expect(screen.getByText("Active")).toBeInTheDocument();
  });

  it("shows the em-dash placeholder when there is no account yet", () => {
    billingAccount = null;
    render(<OverviewPanel />);
    // The InfoRow wrapper (label + value as siblings) — matches the value
    // span rendered next to the "Subscription" label specifically, since
    // other InfoRows in this panel have real mocked values.
    const row = screen.getByText("Subscription").closest(".justify-between");
    expect(row?.textContent).toContain("—");
  });

  it("reads Free tier ended once the free-tier window has elapsed", () => {
    billingAccount = {
      subscription_status: "trial_local",
      trial_end: NOW / 1000 - 86_400, // 1 day before the mocked "now" — expired
    };
    render(<OverviewPanel />);
    expect(screen.getByText("Free tier ended")).toBeInTheDocument();
    expect(screen.queryByText("Free tier")).not.toBeInTheDocument();
  });

  it("stays Free tier while the free-tier window is still open", () => {
    billingAccount = {
      subscription_status: "trial_local",
      trial_end: NOW / 1000 + 86_400, // 1 day after the mocked "now" — not expired
    };
    render(<OverviewPanel />);
    expect(screen.getByText("Free tier")).toBeInTheDocument();
    expect(screen.queryByText("Free tier ended")).not.toBeInTheDocument();
  });
});
