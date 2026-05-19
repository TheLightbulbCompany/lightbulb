import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";

import RefreshPage from "../refresh/page";
import ReturnPage from "../return/page";

const mockUseOrganization = vi.fn(() => ({ membership: null as null | { role: string } }));
vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({
    isSignedIn: true,
    getToken: vi.fn().mockResolvedValue("test-token"),
  }),
  useOrganization: () => mockUseOrganization(),
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
}));

// Global fetch stub — these pages call window.fetch directly, not useApi.
// We assert the gated paths NEVER invoke it.
beforeEach(() => {
  globalThis.fetch = vi.fn(() => Promise.resolve(new Response("{}"))) as typeof fetch;
  mockUseOrganization.mockReturnValue({ membership: null });
});

describe("PayoutsRefreshPage — org-admin gate", () => {
  it("renders the restricted view for non-admin org members and never fetches", () => {
    mockUseOrganization.mockReturnValue({ membership: { role: "org:member" } });
    render(<RefreshPage />);
    expect(screen.getByTestId("marketplace-restricted")).toBeInTheDocument();
    expect(globalThis.fetch).not.toHaveBeenCalled();
  });

  it("does NOT render the restricted view for personal users", () => {
    mockUseOrganization.mockReturnValue({ membership: null });
    render(<RefreshPage />);
    expect(screen.queryByTestId("marketplace-restricted")).toBeNull();
    expect(screen.getByText(/Refreshing Stripe onboarding/i)).toBeInTheDocument();
  });

  it("does NOT render the restricted view for org admins", () => {
    mockUseOrganization.mockReturnValue({ membership: { role: "org:admin" } });
    render(<RefreshPage />);
    expect(screen.queryByTestId("marketplace-restricted")).toBeNull();
    expect(screen.getByText(/Refreshing Stripe onboarding/i)).toBeInTheDocument();
  });
});

describe("PayoutsReturnPage — org-admin gate", () => {
  it("renders the restricted view for non-admin org members and never fetches", () => {
    mockUseOrganization.mockReturnValue({ membership: { role: "org:member" } });
    render(<ReturnPage />);
    expect(screen.getByTestId("marketplace-restricted")).toBeInTheDocument();
    expect(globalThis.fetch).not.toHaveBeenCalled();
  });

  it("does NOT render the restricted view for personal users", () => {
    mockUseOrganization.mockReturnValue({ membership: null });
    render(<ReturnPage />);
    expect(screen.queryByTestId("marketplace-restricted")).toBeNull();
    expect(screen.getByText(/Finishing setup/i)).toBeInTheDocument();
  });

  it("does NOT render the restricted view for org admins", () => {
    mockUseOrganization.mockReturnValue({ membership: { role: "org:admin" } });
    render(<ReturnPage />);
    expect(screen.queryByTestId("marketplace-restricted")).toBeNull();
    expect(screen.getByText(/Finishing setup/i)).toBeInTheDocument();
  });
});
