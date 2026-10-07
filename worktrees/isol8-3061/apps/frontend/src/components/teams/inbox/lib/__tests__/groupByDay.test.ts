import { describe, expect, it } from "vitest";
import { groupByDay } from "@/components/teams/inbox/lib/groupByDay";

const NOW = new Date("2026-09-08T12:00:00Z");
const at = (iso: string) => ({ createdAt: iso });

describe("groupByDay", () => {
  it("labels today and yesterday by name", () => {
    const groups = groupByDay(
      [at("2026-09-08T09:00:00Z"), at("2026-09-07T09:00:00Z")],
      (e) => e.createdAt,
      NOW,
    );
    expect(groups.map((g) => g.label)).toEqual(["Today", "Yesterday"]);
  });

  it("keeps same-day items in one group, newest group first", () => {
    const groups = groupByDay(
      [at("2026-09-08T09:00:00Z"), at("2026-09-08T08:00:00Z"), at("2026-09-06T08:00:00Z")],
      (e) => e.createdAt,
      NOW,
    );
    expect(groups).toHaveLength(2);
    expect(groups[0].items).toHaveLength(2);
  });

  it("dates anything older than yesterday", () => {
    const groups = groupByDay([at("2026-09-01T09:00:00Z")], (e) => e.createdAt, NOW);
    expect(groups[0].label).not.toMatch(/today|yesterday/i);
    expect(groups[0].label).toMatch(/Sep/);
  });

  it("drops items with an unparseable timestamp rather than inventing a day", () => {
    expect(groupByDay([at("not-a-date")], (e) => e.createdAt, NOW)).toEqual([]);
  });
});
