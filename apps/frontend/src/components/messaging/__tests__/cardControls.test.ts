import { describe, expect, it } from "vitest";
import { cardControls } from "../cardControls";

describe("cardControls", () => {
  it("a normal room gets cards and replies", () => expect(cardControls({ feed: false, archivedAt: null })).toEqual({ cardsEnabled: true, repliesEnabled: true }));
  it("the feed gets neither (fences stay code)", () => expect(cardControls({ feed: true, archivedAt: null })).toEqual({ cardsEnabled: false, repliesEnabled: false }));
  it("an archived room gets read-only cards", () => expect(cardControls({ feed: false, archivedAt: "2026-10-01T00:00:00Z" })).toEqual({ cardsEnabled: true, repliesEnabled: false }));
  it("an unknown room gets read-only cards", () => expect(cardControls(undefined)).toEqual({ cardsEnabled: true, repliesEnabled: false }));
});
