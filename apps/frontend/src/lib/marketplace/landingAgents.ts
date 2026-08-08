export type LandingMarketplaceAgent = {
  id: "L" | "St" | "O" | "T" | "E";
  slug: "lens" | "stitch" | "ora" | "tally" | "echo";
  listingId: string;
  name: string;
  role: string;
  value: string;
  tags: string[];
};

// Marketplace showcase data for the landing-page constellation. These rows are
// pinned to real published marketplace listings so the landing page does not
// drift into invented agents or stale routes.
export const landingMarketplaceAgents: LandingMarketplaceAgent[] = [
  {
    id: "L",
    slug: "lens",
    listingId: "af8e37fc-2230-4b55-bc57-b5add0f74087",
    name: "Lens",
    role: "Research agent",
    value:
      "Searches trusted research sources, reads the strongest papers, and writes a clear cited summary.",
    tags: ["research", "literature", "arxiv", "pubmed"],
  },
  {
    id: "St",
    slug: "stitch",
    listingId: "1e21c71b-b2f6-472f-bd62-71826216d8ce",
    name: "Stitch",
    role: "Sales agent",
    value:
      "Finds qualified buyers, drafts personalized outreach, and keeps your CRM up to date.",
    tags: ["sales", "prospecting", "outreach", "crm", "leads"],
  },
  {
    id: "O",
    slug: "ora",
    listingId: "9db9bb0f-da17-43ec-b636-2e82e5362aea",
    name: "Ora",
    role: "Scheduling agent",
    value:
      "Coordinates meetings, protects your calendar, and brings schedule decisions back to you.",
    tags: ["calendar", "scheduling", "meetings", "time", "assistant"],
  },
  {
    id: "T",
    slug: "tally",
    listingId: "bd538177-01ed-4bb6-b8bc-8eb236308780",
    name: "Tally",
    role: "Finance agent",
    value:
      "Keeps books current with categorization, reconciliation, anomaly checks, and tax-prep support.",
    tags: ["finance", "accounting", "tax"],
  },
  {
    id: "E",
    slug: "echo",
    listingId: "62c7d6ac-980e-41ff-b166-ba7c5d6ed762",
    name: "Echo",
    role: "Meeting recap agent",
    value:
      "Turns meetings into accurate recaps, attributed decisions, action items, and follow-through.",
    tags: ["meetings", "recap", "crm", "hubspot", "notes"],
  },
];

export function marketplaceListingHref(listingId: string): string {
  return `/marketplace/listing/${listingId}`;
}
