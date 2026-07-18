import { describe, it, expect } from "vitest";
import {
  parseSearchQuery,
  buildSearchQueryString,
  hasSearchFilters,
  searchOperatorSuggestions,
  applySearchOperatorSuggestion,
} from "../searchQueryParser";

const CTX = {
  currentAgentId: "agent-me",
  currentUserId: "user-me",
  agents: [{ id: "a1", name: "Friday", urlKey: "friday" }],
  projects: [{ id: "p1", name: "Acme App", urlKey: "acme-app" }],
  labels: [{ id: "l1", name: "bug" }],
};

describe("parseSearchQuery", () => {
  it("returns residual free text when there are no operators", () => {
    const parsed = parseSearchQuery("broken   invoice email");
    expect(parsed.query).toBe("broken invoice email");
    expect(parsed.filters).toEqual({});
    expect(parsed.pills).toEqual([]);
  });

  it("parses status + priority operators and keeps residual text", () => {
    const parsed = parseSearchQuery("login status:todo priority:high bug", CTX);
    expect(parsed.query).toBe("login bug");
    expect(parsed.filters.status).toEqual(["todo"]);
    expect(parsed.filters.priority).toEqual(["high"]);
    expect(parsed.pills.map((p) => p.label)).toEqual(["status:todo", "priority:high"]);
  });

  it("accumulates repeated status operators uniquely", () => {
    const parsed = parseSearchQuery("status:todo status:in_progress status:todo");
    expect(parsed.filters.status).toEqual(["todo", "in_progress"]);
  });

  it("expands is:open / is:closed to status sets", () => {
    expect(parseSearchQuery("is:open").filters.status).toEqual([
      "backlog",
      "todo",
      "in_progress",
      "in_review",
      "blocked",
    ]);
    expect(parseSearchQuery("is:closed").filters.status).toEqual(["done", "cancelled"]);
  });

  it("resolves assignee:me to the current agent id when present", () => {
    const parsed = parseSearchQuery("assignee:me", CTX);
    expect(parsed.filters.assigneeAgentId).toBe("agent-me");
    expect(parsed.filters.assigneeUserId).toBeUndefined();
  });

  it("falls back to the me sentinel when no board identity is resolved", () => {
    const parsed = parseSearchQuery("assignee:me");
    expect(parsed.filters.assigneeUserId).toBe("me");
    expect(parsed.filters.assigneeAgentId).toBeUndefined();
  });

  it("resolves assignee by agent name", () => {
    const parsed = parseSearchQuery("assignee:friday", CTX);
    expect(parsed.filters.assigneeAgentId).toBe("a1");
    expect(parsed.pills[0]).toMatchObject({ key: "assignee", label: "assignee:Friday" });
  });

  it("resolves a quoted multi-word project name to its id", () => {
    const parsed = parseSearchQuery('project:"Acme App" report', CTX);
    expect(parsed.filters.projectId).toBe("p1");
    expect(parsed.query).toBe("report");
  });

  it("resolves a label by name and by raw uuid", () => {
    expect(parseSearchQuery("label:bug", CTX).filters.labelId).toBe("l1");
    const uuid = "12345678-1234-1234-1234-123456789012";
    expect(parseSearchQuery(`label:${uuid}`).filters.labelId).toBe(uuid);
  });

  it("parses updated:>7d into updatedWithin", () => {
    const parsed = parseSearchQuery("updated:>7d");
    expect(parsed.filters.updatedWithin).toBe("7d");
    expect(parsed.pills[0]).toMatchObject({ key: "updated", label: "updated:>7d" });
  });

  it("keeps unrecognized operator values as literal text", () => {
    const parsed = parseSearchQuery("status:banana priority:urgent", CTX);
    expect(parsed.filters.status).toBeUndefined();
    expect(parsed.filters.priority).toBeUndefined();
    expect(parsed.query).toBe("status:banana priority:urgent");
  });

  it("parses BFF-only statuses (backlog, cancelled) as real filters", () => {
    expect(parseSearchQuery("status:backlog").filters.status).toEqual(["backlog"]);
    const parsed = parseSearchQuery("status:cancelled report");
    expect(parsed.filters.status).toEqual(["cancelled"]);
    expect(parsed.query).toBe("report");
  });

  it("drops board-only statuses the search BFF rejects to literal text", () => {
    // pending / review / won_t_do / duplicate are valid board columns but are
    // NOT in the /teams/search allow-list (_ISSUE_STATUSES) — serializing them
    // as a status filter would 422 ("Search failed"), so they must remain
    // residual free text instead of an invalid filter.
    for (const status of ["pending", "review", "won_t_do", "duplicate"]) {
      const parsed = parseSearchQuery(`status:${status}`);
      expect(parsed.filters.status).toBeUndefined();
      expect(parsed.query).toBe(`status:${status}`);
    }
  });
});

describe("buildSearchQueryString", () => {
  it("emits q plus repeated multi-value filter keys", () => {
    const qs = buildSearchQueryString("invoice", {
      status: ["todo", "done"],
      priority: ["high"],
    });
    const params = new URLSearchParams(qs);
    expect(params.get("q")).toBe("invoice");
    expect(params.getAll("status")).toEqual(["todo", "done"]);
    expect(params.getAll("priority")).toEqual(["high"]);
  });

  it("serializes a null assignee filter as the null sentinel", () => {
    const params = new URLSearchParams(buildSearchQueryString("", { assigneeAgentId: null }));
    expect(params.get("assigneeAgentId")).toBe("null");
    expect(params.get("q")).toBeNull();
  });

  it("round-trips the operators parsed from a query", () => {
    const parsed = parseSearchQuery("bug status:todo assignee:friday", CTX);
    const params = new URLSearchParams(buildSearchQueryString(parsed.query, parsed.filters));
    expect(params.get("q")).toBe("bug");
    expect(params.getAll("status")).toEqual(["todo"]);
    expect(params.get("assigneeAgentId")).toBe("a1");
  });
});

describe("hasSearchFilters", () => {
  it("is false for an empty filter set and true when any filter is set", () => {
    expect(hasSearchFilters({})).toBe(false);
    expect(hasSearchFilters({ status: ["todo"] })).toBe(true);
    expect(hasSearchFilters({ assigneeAgentId: null })).toBe(true);
    expect(hasSearchFilters({ updatedWithin: "7d" })).toBe(true);
  });
});

describe("operator suggestions", () => {
  it("filters suggestions by the current token prefix", () => {
    const suggestions = searchOperatorSuggestions("status:");
    expect(suggestions.length).toBeGreaterThan(0);
    expect(suggestions.every((s) => s.token.startsWith("status:"))).toBe(true);
  });

  it("replaces the trailing token when applying a suggestion", () => {
    expect(applySearchOperatorSuggestion("login stat", "status:todo")).toBe("login status:todo");
  });
});
