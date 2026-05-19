import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";

import { HandlePicker } from "../HandlePicker";

// Default to personal user — tests override via mockUseOrganization.
const mockUseOrganization = vi.fn(() => ({ membership: null as null | { role: string } }));
vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({ getToken: vi.fn().mockResolvedValue("test-token") }),
  useOrganization: () => mockUseOrganization(),
}));

// Stub the API surface — HandlePicker imports these eagerly but the gate
// short-circuits before any of them run.
vi.mock("@/lib/marketplace/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/marketplace/api")>(
    "@/lib/marketplace/api",
  );
  return {
    ...actual,
    checkHandleAvailability: vi.fn(),
    getMySellerProfile: vi.fn(),
    upsertMySellerProfile: vi.fn(),
  };
});

describe("HandlePicker — org-admin gate", () => {
  beforeEach(() => {
    mockUseOrganization.mockReturnValue({ membership: null });
  });

  it("renders the input form for personal users", () => {
    render(<HandlePicker />);
    expect(screen.getByLabelText(/Marketplace handle/i)).toBeInTheDocument();
    expect(screen.queryByTestId("marketplace-restricted")).toBeNull();
  });

  it("renders the input form for org admins", () => {
    mockUseOrganization.mockReturnValue({ membership: { role: "org:admin" } });
    render(<HandlePicker />);
    expect(screen.getByLabelText(/Marketplace handle/i)).toBeInTheDocument();
    expect(screen.queryByTestId("marketplace-restricted")).toBeNull();
  });

  it("renders the restricted view for non-admin org members", () => {
    mockUseOrganization.mockReturnValue({ membership: { role: "org:member" } });
    render(<HandlePicker />);
    expect(screen.getByTestId("marketplace-restricted")).toBeInTheDocument();
    // No input — the gate replaces the entire form.
    expect(screen.queryByLabelText(/Marketplace handle/i)).toBeNull();
  });
});
