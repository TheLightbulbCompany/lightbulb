import { describe, it, expect, vi, beforeEach } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";

import { CompanyListingCard } from "../CompanyListingCard";
import { deployCompany } from "@/lib/marketplace/api";
import type { Listing } from "@/lib/marketplace/types";

vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({ getToken: vi.fn().mockResolvedValue("tok_test") }),
}));

const isAdmin = vi.hoisted(() => ({ value: true }));
vi.mock("@/hooks/useIsWorkspaceAdmin", () => ({ useIsWorkspaceAdmin: () => isAdmin.value }));

vi.mock("@/lib/marketplace/api", () => ({
  deployCompany: vi.fn(),
}));

function companyListing(minPlan: "standard" | "ultra_pro"): Listing {
  return {
    listing_id: "l1",
    version: 1,
    slug: "gstack",
    name: "GStack",
    description_md: "Engineering company",
    format: "company",
    price_cents: 0,
    tags: ["company"],
    seller_id: "__isol8__",
    status: "published",
    created_at: "2026-07-01T00:00:00Z",
    published_at: "2026-07-01T00:00:00Z",
    manifest_json: {
      company: {
        slug: "gstack",
        name: "GStack",
        description: "Engineering org in a box",
        agent_count: 5,
        team_count: 1,
        skill_count: 12,
        agent_slugs: ["ceo", "eng"],
        env_requirements: [],
        min_plan: minPlan,
      },
    },
  } as unknown as Listing;
}

beforeEach(() => {
  vi.mocked(deployCompany).mockReset();
  isAdmin.value = true;
});

describe("<CompanyListingCard>", () => {
  it("deploys on click and flips to the check-Teams state", async () => {
    vi.mocked(deployCompany).mockResolvedValueOnce({ started: true, agent_count: 5 });
    render(<CompanyListingCard listing={companyListing("standard")} />);

    fireEvent.click(screen.getByRole("button", { name: /deploy company/i }));

    expect(await screen.findByRole("status")).toHaveTextContent(/check teams/i);
    expect(deployCompany).toHaveBeenCalledWith(expect.objectContaining({ listingId: "l1" }));
    expect(screen.queryByRole("button", { name: /deploy company/i })).toBeNull();
  });

  it("legacy Ultra metadata and errors do not render an upgrade prompt", async () => {
    vi.mocked(deployCompany).mockRejectedValueOnce(new Error("Requires Ultra"));
    render(<CompanyListingCard listing={companyListing("ultra_pro")} />);

    fireEvent.click(screen.getByRole("button", { name: /deploy company/i }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/requires ultra/i);
    expect(screen.queryByText(/^ultra$/i)).toBeNull();
    expect(screen.queryByRole("link", { name: /upgrade in settings/i })).toBeNull();
    expect(screen.getByRole("button", { name: /deploy company/i })).toBeEnabled();
  });

  it("disables Deploy company for non-admins with a reason", () => {
    isAdmin.value = false;
    render(<CompanyListingCard listing={companyListing("standard")} />);
    const btn = screen.getByRole("button", { name: /deploy company/i });
    expect(btn).toBeDisabled();
    expect(btn).toHaveAttribute("title", expect.stringMatching(/admins/i));
  });
});
