import { describe, test, expect } from "vitest";
import {
  buildGroupedInboxSections,
  getInboxWorkItems,
  matchesInboxIssueSearch,
  type InboxGroupedSection,
  type InboxWorkItem,
} from "@/components/teams/shared/lib/inbox";
import type { Issue } from "@/components/teams/shared/types";

function makeIssue(overrides: Partial<Issue> = {}): Issue {
  return {
    id: overrides.id ?? "issue-1",
    title: overrides.title ?? "Untitled",
    status: overrides.status ?? "todo",
    ...overrides,
  };
}

describe("getInboxWorkItems", () => {
  test("wraps each issue in {kind:'issue', issue}", () => {
    const i1 = makeIssue({ id: "a" });
    const i2 = makeIssue({ id: "b" });
    expect(getInboxWorkItems([i1, i2])).toEqual([
      { kind: "issue", issue: i1 },
      { kind: "issue", issue: i2 },
    ]);
  });

  test("returns empty array for empty input", () => {
    expect(getInboxWorkItems([])).toEqual([]);
  });

  test("preserves input order", () => {
    const issues = ["c", "a", "b"].map((id) => makeIssue({ id }));
    const ids = getInboxWorkItems(issues).map((item) => item.issue.id);
    expect(ids).toEqual(["c", "a", "b"]);
  });
});

describe("matchesInboxIssueSearch", () => {
  test("returns true on empty / whitespace query", () => {
    const i = makeIssue({ title: "Anything" });
    expect(matchesInboxIssueSearch(i, "")).toBe(true);
    expect(matchesInboxIssueSearch(i, "   ")).toBe(true);
  });

  test("matches title case-insensitively", () => {
    const i = makeIssue({ title: "Fix Inbox keyboard nav" });
    expect(matchesInboxIssueSearch(i, "fix")).toBe(true);
    expect(matchesInboxIssueSearch(i, "INBOX")).toBe(true);
    expect(matchesInboxIssueSearch(i, "ship")).toBe(false);
  });

  test("matches identifier when present", () => {
    const i = makeIssue({ title: "Whatever", identifier: "ENG-123" });
    expect(matchesInboxIssueSearch(i, "eng-123")).toBe(true);
    expect(matchesInboxIssueSearch(i, "eng-999")).toBe(false);
  });

  test("does not crash when identifier / title are missing-ish", () => {
    const i = makeIssue({ title: "", identifier: null });
    expect(matchesInboxIssueSearch(i, "foo")).toBe(false);
    expect(matchesInboxIssueSearch(i, "")).toBe(true);
  });
});

describe("buildGroupedInboxSections", () => {
  const NOW = "2026-05-04T15:00:00.000Z";

  test("returns empty sections for empty input", () => {
    expect(buildGroupedInboxSections([], { nowIso: NOW })).toEqual([]);
  });

  test("returns a single 'search' section when searchQuery is non-empty", () => {
    const items: InboxWorkItem[] = [
      { kind: "issue", issue: makeIssue({ id: "1" }) },
      { kind: "issue", issue: makeIssue({ id: "2" }) },
    ];
    const sections = buildGroupedInboxSections(items, {
      nowIso: NOW,
      searchQuery: "foo",
    });
    expect(sections).toHaveLength(1);
    expect(sections[0].kind).toBe("search");
    expect(sections[0].items).toHaveLength(2);
  });

  test("treats whitespace-only searchQuery as no search", () => {
    const items: InboxWorkItem[] = [
      {
        kind: "issue",
        issue: makeIssue({ id: "1", updatedAt: "2026-05-04T16:00:00.000Z" }),
      },
    ];
    const sections = buildGroupedInboxSections(items, {
      nowIso: NOW,
      searchQuery: "   ",
    });
    expect(sections.map((s) => s.kind)).toEqual(["today"]);
  });

  test("splits items into today vs earlier by updatedAt (rolling 24h window)", () => {
    // NOW = 2026-05-04T15:00:00Z → cutoff = 2026-05-03T15:00:00Z.
    // "today" = items strictly within the last 24h. TZ-agnostic.
    const items: InboxWorkItem[] = [
      {
        kind: "issue",
        issue: makeIssue({ id: "today-1", updatedAt: "2026-05-04T08:00:00.000Z" }),
      },
      {
        kind: "issue",
        issue: makeIssue({ id: "today-2", updatedAt: "2026-05-03T20:00:00.000Z" }),
      },
      // earlier (older than 24h from NOW)
      {
        kind: "issue",
        issue: makeIssue({ id: "old-1", updatedAt: "2026-05-03T10:00:00.000Z" }),
      },
      {
        kind: "issue",
        issue: makeIssue({ id: "old-2", updatedAt: "2026-04-01T08:00:00.000Z" }),
      },
    ];
    const sections = buildGroupedInboxSections(items, { nowIso: NOW });
    expect(sections.map((s) => s.kind)).toEqual(["today", "earlier"]);
    expect(sections[0].items.map((i) => i.issue.id)).toEqual([
      "today-1",
      "today-2",
    ]);
    expect(sections[1].items.map((i) => i.issue.id)).toEqual(["old-1", "old-2"]);
  });

  test("falls back to lastActivityAt when updatedAt missing", () => {
    const items: InboxWorkItem[] = [
      {
        kind: "issue",
        issue: makeIssue({
          id: "fallback",
          updatedAt: null,
          lastActivityAt: "2026-05-04T09:00:00.000Z",
        }),
      },
    ];
    const sections = buildGroupedInboxSections(items, { nowIso: NOW });
    expect(sections.map((s) => s.kind)).toEqual(["today"]);
  });

  test("omits empty sections", () => {
    const items: InboxWorkItem[] = [
      {
        kind: "issue",
        issue: makeIssue({ id: "1", updatedAt: "2026-05-04T10:00:00.000Z" }),
      },
    ];
    const sections = buildGroupedInboxSections(items, { nowIso: NOW });
    expect(sections.map((s) => s.kind)).toEqual(["today"]);
  });
});
