import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";

import Dashboard from "../page";

const mockApiGet = vi.fn();
const mockApiPost = vi.fn();
vi.mock("@/lib/api", () => ({
  useApi: () => ({
    get: mockApiGet,
    post: mockApiPost,
    put: vi.fn(),
    del: vi.fn(),
  }),
}));

const mockUseOrganization = vi.fn(() => ({ membership: null as null | { role: string } }));
vi.mock("@clerk/nextjs", () => ({
  useOrganization: () => mockUseOrganization(),
}));

// SWR has a global cache that persists across renders. Wrap each test in a
// fresh SWRConfig with `provider: () => new Map()` to keep caches isolated.
import { SWRConfig } from "swr";
function renderDashboard() {
  return render(
    <SWRConfig value={{ provider: () => new Map() }}>
      <Dashboard />
    </SWRConfig>,
  );
}

describe("Marketplace creator dashboard — org-admin gate", () => {
  beforeEach(() => {
    mockApiGet.mockReset();
    mockApiPost.mockReset();
    mockUseOrganization.mockReturnValue({ membership: null });
  });

  it("renders the restricted view for non-admin org members and skips the fetch", async () => {
    mockUseOrganization.mockReturnValue({ membership: { role: "org:member" } });
    renderDashboard();
    expect(await screen.findByTestId("marketplace-restricted")).toBeInTheDocument();
    // SWR is keyed null → no GET. This is the load-bearing assertion: if we
    // ever forget to short-circuit the SWR key, the page would 403 in the
    // background and the seller would see noise.
    expect(mockApiGet).not.toHaveBeenCalled();
  });

  it("does NOT render the restricted view for personal users", async () => {
    mockUseOrganization.mockReturnValue({ membership: null });
    mockApiGet.mockResolvedValue({ balance_held_cents: 0 });
    renderDashboard();
    // Pre-fetch render is the "Loading…" placeholder; the gate isn't shown.
    expect(screen.queryByTestId("marketplace-restricted")).toBeNull();
    expect(screen.getByText(/Loading/i)).toBeInTheDocument();
  });

  it("does NOT render the restricted view for org admins", async () => {
    mockUseOrganization.mockReturnValue({ membership: { role: "org:admin" } });
    mockApiGet.mockResolvedValue({ balance_held_cents: 0 });
    renderDashboard();
    expect(screen.queryByTestId("marketplace-restricted")).toBeNull();
    expect(screen.getByText(/Loading/i)).toBeInTheDocument();
  });
});
