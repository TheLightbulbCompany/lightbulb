import { describe, expect, it } from "vitest";
import { initialsFor, paletteFor, fromListing, PALETTE } from "../agentDesign";
import type { Listing } from "@/lib/marketplace/types";

describe("initialsFor", () => {
  it("uses first letter of two words for multi-word names", () => {
    expect(initialsFor("Inbox Triage")).toBe("IT");
  });

  it("uses first two characters for single-word names", () => {
    expect(initialsFor("Atlas")).toBe("At");
  });

  it("never returns an empty string", () => {
    expect(initialsFor("")).toBe("Ag");
  });
});

describe("paletteFor", () => {
  it("returns the same palette for the same slug", () => {
    expect(paletteFor("atlas")).toEqual(paletteFor("atlas"));
  });

  it("returns the single neutral chip style for any slug — agents are not colour-coded, identity is the monogram", () => {
    expect(paletteFor("anything")).toEqual({ accent: "var(--foreground)", tint: "var(--accent)" });
    expect(paletteFor("a totally different slug")).toEqual({ accent: "var(--foreground)", tint: "var(--accent)" });
  });

  it("never uses a state token (--ok/--warn/--err/--info) for agent identity", () => {
    for (const entry of PALETTE) {
      expect(entry.accent).not.toMatch(/--(ok|warn|err|info)\b/);
      expect(entry.tint).not.toMatch(/--(ok|warn|err|info)\b/);
    }
  });
});

describe("fromListing", () => {
  const sampleListing: Listing = {
    listing_id: "id1",
    slug: "atlas",
    name: "Atlas",
    description_md: "Clears your inbox before standup.\n\nDrafts, defers, dispatches.",
    format: "openclaw",
    price_cents: 0,
    tags: ["communication", "ops"],
    seller_id: "seller1",
    status: "published",
    version: 1,
    published_at: null,
    created_at: "2026-05-01",
  };

  it("formats free listings as 'Free'", () => {
    expect(fromListing(sampleListing).price).toBe("Free");
    expect(fromListing(sampleListing).isFree).toBe(true);
  });

  it("formats paid listings with the dollar amount", () => {
    expect(fromListing({ ...sampleListing, price_cents: 1499 }).price).toBe("$14.99");
  });

  it("derives the vibe from the first sentence of the description", () => {
    expect(fromListing(sampleListing).vibe).toBe("Clears your inbox before standup.");
  });

  it("uses the first tag as the role label", () => {
    expect(fromListing(sampleListing).role).toBe("communication");
  });

  it("falls back to 'Agent' role when no tags are present", () => {
    expect(fromListing({ ...sampleListing, tags: [] }).role).toBe("Agent");
  });
});
