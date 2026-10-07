import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { CompanyCard, companyMeta } from "../storefront/Listing/CompanyCard";
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
    expect(screen.getByText(/one-click deploy/i)).toBeInTheDocument();
  });

  it("does not treat legacy min_plan metadata as a deploy entitlement", () => {
    render(<CompanyCard listing={companyListing(49, "ultra_pro")} />);
    expect(screen.getByText(/one-click deploy/i)).toBeInTheDocument();
    expect(screen.queryByText(/requires ultra/i)).toBeNull();
  });
});

describe("companyMeta", () => {
  it("returns the company block for a company-format listing", () => {
    expect(companyMeta(companyListing(5, "standard"))?.agent_count).toBe(5);
  });

  it("returns null for a non-company listing even if it carries a stray company key", () => {
    // Guard against a mis-shaped agent row hijacking the detail page into
    // company mode (disabled deploy button + CompanyFacts deref).
    const agentRow = {
      listing_id: "l2", version: 1, slug: "some-agent", name: "Some Agent",
      description_md: "An agent", format: "openclaw", price_cents: 0,
      tags: [], seller_id: "seller_1", status: "published",
      manifest_json: { company: { min_plan: "standard" } },
    } as unknown as Listing;
    expect(companyMeta(agentRow)).toBeNull();
  });
});
