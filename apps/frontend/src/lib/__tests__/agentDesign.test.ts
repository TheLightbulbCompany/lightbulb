import { describe, expect, it } from "vitest";
import { initialsFor, paletteFor, fromListing } from "../agentDesign";
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

  it("returns a palette entry from the curated set", () => {
    const result = paletteFor("anything");
    expect(result.accent).toMatch(/^#[0-9a-f]{6}$/i);
    expect(result.tint).toMatch(/^#[0-9a-f]{6}$/i);
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
