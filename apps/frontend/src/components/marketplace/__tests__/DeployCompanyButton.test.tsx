import { describe, it, expect, vi, beforeEach } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import { DeployCompanyButton } from "../company/DeployCompanyButton";
import { deployCompany, getCompanyDeploy } from "@/lib/marketplace/api";
import type { CompanyDeployJob, Listing } from "@/lib/marketplace/types";

// Stable auth object — DeployProgress lists getToken in a useEffect dep, so a
// fresh function per render would re-fire the poll loop indefinitely (same trap
// the ListingDetailView tests document).
const { stableAuth } = vi.hoisted(() => ({
  stableAuth: {
    isLoaded: true,
    isSignedIn: true as boolean,
    userId: "owner_1" as string | null,
    getToken: async (): Promise<string | null> => "test-jwt",
  },
}));
vi.mock("@clerk/nextjs", () => ({
  useAuth: () => stableAuth,
}));

vi.mock("@/lib/marketplace/api", () => ({
  deployCompany: vi.fn(),
  getCompanyDeploy: vi.fn(),
  rollbackCompanyDeploy: vi.fn(),
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
        description: "Engineering company",
        license: "MIT",
        authors: [],
        agent_count: minPlan === "standard" ? 5 : 40,
        team_count: 1,
        skill_count: 12,
        agent_slugs: ["ceo", "eng"],
        env_requirements: [],
        min_plan: minPlan,
      },
    },
  } as unknown as Listing;
}

const runningJob: CompanyDeployJob = {
  job_id: "cdpl_1",
  status: "running",
  agents: [{ slug: "ceo", paperclip_id: null, state: "pending" }],
  routine_ids: [],
  error: null,
};

beforeEach(() => {
  vi.mocked(deployCompany).mockReset();
  vi.mocked(getCompanyDeploy).mockReset();
  // Keep the post-deploy poll harmless — a still-running job with a pending
  // agent, so DeployProgress renders without reaching completed/failed.
  vi.mocked(getCompanyDeploy).mockResolvedValue(runningJob);
  stableAuth.isSignedIn = true;
  stableAuth.userId = "owner_1";
});

describe("<DeployCompanyButton>", () => {
  it("standard company: renders an enabled Deploy button that calls deployCompany", async () => {
    vi.mocked(deployCompany).mockResolvedValueOnce({ job_id: "cdpl_1" });
    render(<DeployCompanyButton listing={companyListing("standard")} />);

    const btn = screen.getByRole("button", { name: /deploy company/i });
    expect(btn).toBeEnabled();

    fireEvent.click(btn);

    await waitFor(() =>
      expect(deployCompany).toHaveBeenCalledWith(
        expect.objectContaining({ listingId: "l1" }),
      ),
    );
  });

  it("ultra_pro company: renders the Requires Ultra Pro upsell and never deploys", () => {
    render(<DeployCompanyButton listing={companyListing("ultra_pro")} />);

    expect(screen.getByText(/requires ultra pro/i)).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /deploy company/i }),
    ).toBeNull();
    expect(deployCompany).not.toHaveBeenCalled();
  });
});
