import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

import { HireManagerDialog } from "../HireManagerDialog";
import type { Listing } from "@/lib/marketplace/types";

// Mock the Teams API read() to feed the manager dropdown.
const mockRead = vi.fn();
vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({ read: mockRead }),
}));

const AGENTS = [
  { id: "friday-id", name: "Friday", role: "ceo" },
  { id: "atlas-id", name: "Atlas", role: "general", title: "Recruiter" },
];

function listing(overrides: Partial<Listing> = {}): Listing {
  return {
    listing_id: "lst_1",
    slug: "demo",
    name: "Demo",
    description_md: "x",
    format: "openclaw",
    price_cents: 0,
    tags: [],
    seller_id: "u_other",
    status: "published",
    version: 1,
    published_at: null,
    created_at: "2026-06-01T00:00:00Z",
    ...overrides,
  };
}

describe("HireManagerDialog", () => {
  beforeEach(() => {
    mockRead.mockReset();
    mockRead.mockReturnValue({ data: { agents: AGENTS }, isLoading: false });
  });

  it("defaults the manager to the CEO and hires with that id", () => {
    const onConfirm = vi.fn();
    render(
      <HireManagerDialog listing={listing()} isHiring={false} onConfirm={onConfirm} onClose={vi.fn()} />,
    );
    const select = screen.getByTestId("reports-to-select") as HTMLSelectElement;
    expect(select.value).toBe("friday-id"); // CEO default
    fireEvent.click(screen.getByTestId("confirm-hire"));
    expect(onConfirm).toHaveBeenCalledWith("friday-id");
  });

  it("lets the buyer pick another agent", () => {
    const onConfirm = vi.fn();
    render(
      <HireManagerDialog listing={listing()} isHiring={false} onConfirm={onConfirm} onClose={vi.fn()} />,
    );
    fireEvent.change(screen.getByTestId("reports-to-select"), { target: { value: "atlas-id" } });
    fireEvent.click(screen.getByTestId("confirm-hire"));
    expect(onConfirm).toHaveBeenCalledWith("atlas-id");
  });

  it("supports 'No manager' (null reports_to)", () => {
    const onConfirm = vi.fn();
    render(
      <HireManagerDialog listing={listing()} isHiring={false} onConfirm={onConfirm} onClose={vi.fn()} />,
    );
    fireEvent.change(screen.getByTestId("reports-to-select"), { target: { value: "" } });
    fireEvent.click(screen.getByTestId("confirm-hire"));
    expect(onConfirm).toHaveBeenCalledWith(null);
  });

  it("Cancel calls onClose without hiring", () => {
    const onConfirm = vi.fn();
    const onClose = vi.fn();
    render(
      <HireManagerDialog listing={listing()} isHiring={false} onConfirm={onConfirm} onClose={onClose} />,
    );
    fireEvent.click(screen.getByText("Cancel"));
    expect(onClose).toHaveBeenCalled();
    expect(onConfirm).not.toHaveBeenCalled();
  });
});
