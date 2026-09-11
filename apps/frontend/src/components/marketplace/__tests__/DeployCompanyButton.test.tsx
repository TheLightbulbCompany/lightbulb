import { describe, it, expect, vi, beforeEach } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import { DeployCompanyButton } from "../company/DeployCompanyButton";
import { deployCompany } from "@/lib/marketplace/api";
import type { Listing } from "@/lib/marketplace/types";

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

beforeEach(() => {
  vi.mocked(deployCompany).mockReset();
  stableAuth.isSignedIn = true;
  stableAuth.userId = "owner_1";
});

describe("<DeployCompanyButton>", () => {
  it("standard company: renders an enabled Deploy button that calls deployCompany and shows the confirmation on success", async () => {
    vi.mocked(deployCompany).mockResolvedValueOnce({ started: true, agent_count: 5 });
    render(<DeployCompanyButton listing={companyListing("standard")} />);

    const btn = screen.getByRole("button", { name: /deploy company/i });
    expect(btn).toBeEnabled();

    fireEvent.click(btn);

    await waitFor(() =>
      expect(deployCompany).toHaveBeenCalledWith(
        expect.objectContaining({ listingId: "l1" }),
      ),
    );

    // On success the button is replaced by a static confirmation card that
    // names the agent count and points the buyer at Teams.
    const card = await screen.findByTestId("deploy-started-card");
    expect(card).toHaveTextContent(/deploying gstack/i);
    expect(card).toHaveTextContent(/5 agents/i);
    expect(screen.getByRole("link", { name: /open teams/i })).toHaveAttribute("href", "/chat");
    expect(screen.queryByRole("button", { name: /deploy company/i })).toBeNull();
  });

  it("legacy ultra_pro metadata does not disable deploy", () => {
    render(<DeployCompanyButton listing={companyListing("ultra_pro")} />);

    expect(screen.getByRole("button", { name: /deploy company/i })).toBeEnabled();
    expect(deployCompany).not.toHaveBeenCalled();
  });

  it("a legacy Requires Ultra error is shown as retryable, without an upgrade prompt", async () => {
    vi.mocked(deployCompany).mockRejectedValueOnce(new Error("Requires Ultra"));
    render(<DeployCompanyButton listing={companyListing("ultra_pro")} />);

    fireEvent.click(screen.getByRole("button", { name: /deploy company/i }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/requires ultra/i);
    expect(screen.queryByRole("link", { name: /upgrade in settings/i })).toBeNull();
    expect(screen.getByRole("button", { name: /try again/i })).toBeInTheDocument();
  });

  it("already-deployed 409 is terminal — message only, no retry, no links", async () => {
    vi.mocked(deployCompany).mockRejectedValueOnce(
      new Error("You already deployed Acme Co — your workspace runs one company package."),
    );
    render(<DeployCompanyButton listing={companyListing("standard")} />);

    fireEvent.click(screen.getByRole("button", { name: /deploy company/i }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/already deployed acme co/i);
    expect(screen.queryByRole("button", { name: /try again/i })).toBeNull();
    expect(screen.queryByRole("link")).toBeNull();
  });

  it("other errors keep the Try again retry", async () => {
    vi.mocked(deployCompany).mockRejectedValueOnce(new Error("Deploy failed."));
    render(<DeployCompanyButton listing={companyListing("standard")} />);

    fireEvent.click(screen.getByRole("button", { name: /deploy company/i }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/deploy failed/i);
    expect(screen.getByRole("button", { name: /try again/i })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /upgrade in settings/i })).toBeNull();
  });
});
