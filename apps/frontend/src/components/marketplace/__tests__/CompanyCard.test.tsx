import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { CompanyCard } from "../storefront/Listing/CompanyCard";
import type { Listing } from "@/lib/marketplace/types";

function companyListing(agentCount: number, minPlan: "standard" | "ultra_pro"): Listing {
  return {
    listing_id: "l1", version: 1, slug: "gstack", name: "GStack",
    description_md: "Engineering company", format: "company", price_cents: 0,
    tags: ["company"], seller_id: "__isol8__", status: "published",
    manifest_json: {
      company: {
        slug: "gstack", name: "GStack", description: "Engineering company",
        license: "MIT", authors: ["Dotta", "Garry Tan"], agent_count: agentCount,
        team_count: 1, skill_count: 27, agent_slugs: [], env_requirements: [],
        min_plan: minPlan,
        source: { repo: "paperclipai/companies", commit: "abc", path: "gstack" },
      },
    },
  } as unknown as Listing;
}

describe("CompanyCard", () => {
  it("shows counts and coming-soon state for standard companies", () => {
    render(<CompanyCard listing={companyListing(5, "standard")} />);
    expect(screen.getByText(/5 agents/i)).toBeInTheDocument();
    expect(screen.getByText(/27 skills/i)).toBeInTheDocument();
    expect(screen.getByText(/coming soon/i)).toBeInTheDocument();
  });

  it("shows the Ultra Pro badge for large companies", () => {
    render(<CompanyCard listing={companyListing(49, "ultra_pro")} />);
    expect(screen.getByText(/requires ultra pro/i)).toBeInTheDocument();
  });
});
