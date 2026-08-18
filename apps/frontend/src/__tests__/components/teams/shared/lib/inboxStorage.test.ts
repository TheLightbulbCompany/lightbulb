import { describe, test, expect, vi } from "vitest";
import {
  loadInboxFilterPreferences,
  saveInboxFilterPreferences,
  saveLastInboxTab,
  loadLastInboxTab,
  loadReadInboxItems,
  saveReadInboxItems,
  inboxStorageKey,
  loadAckRuns,
  saveAckRuns,
  loadBandCollapsed,
  saveBandCollapsed,
} from "@/components/teams/shared/lib/inboxStorage";
import { defaultIssueFilterState } from "@/components/teams/shared/lib/issueFilters";

describe("inboxStorageKey", () => {
  test("namespaces per company", () => {
    expect(inboxStorageKey("co_123", "filters")).toBe("paperclip:inbox:co_123:filters");
  });

  test("namespaces other keys identically", () => {
    expect(inboxStorageKey("co_abc", "tab")).toBe("paperclip:inbox:co_abc:tab");
    expect(inboxStorageKey("co_abc", "read-items")).toBe(
      "paperclip:inbox:co_abc:read-items",
    );
  });
});

describe("loadInboxFilterPreferences", () => {
  test("returns defaults when key missing", () => {
    expect(loadInboxFilterPreferences("co_1")).toEqual({
      issueFilters: defaultIssueFilterState,
    });
  });

  test("returns parsed prefs when key present + valid JSON", () => {
    saveInboxFilterPreferences("co_1", {
      issueFilters: { ...defaultIssueFilterState, statuses: ["todo"] },
    });
    const result = loadInboxFilterPreferences("co_1");
    expect(result.issueFilters.statuses).toEqual(["todo"]);
  });

  test("returns defaults on JSON parse error", () => {
    localStorage.setItem("paperclip:inbox:co_1:filters", "{not valid json");
    expect(loadInboxFilterPreferences("co_1")).toEqual({
      issueFilters: defaultIssueFilterState,
    });
  });

  test("returns defaults when localStorage.getItem throws", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("private mode");
    });
    expect(loadInboxFilterPreferences("co_1")).toEqual({
      issueFilters: defaultIssueFilterState,
    });
  });

  test("merges caller-supplied defaults into the fallback", () => {
    const result = loadInboxFilterPreferences("co_1", {
      issueFilters: { ...defaultIssueFilterState, priorities: ["high"] },
    });
    expect(result.issueFilters.priorities).toEqual(["high"]);
    // Other fields still come from defaultIssueFilterState.
    expect(result.issueFilters.statuses).toEqual([]);
  });

  test("namespaces by companyId — separate companies don't bleed state", () => {
    saveInboxFilterPreferences("co_a", {
      issueFilters: { ...defaultIssueFilterState, statuses: ["done"] },
    });
    expect(loadInboxFilterPreferences("co_b")).toEqual({
      issueFilters: defaultIssueFilterState,
    });
  });
});

describe("saveInboxFilterPreferences", () => {
  test("does not throw when localStorage.setItem throws", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("quota exceeded");
    });
    expect(() =>
      saveInboxFilterPreferences("co_1", { issueFilters: defaultIssueFilterState }),
    ).not.toThrow();
  });
});

describe("loadLastInboxTab / saveLastInboxTab", () => {
  test("round-trips a valid tab", () => {
    saveLastInboxTab("co_1", "recent");
    expect(loadLastInboxTab("co_1")).toBe("recent");
  });

  test("returns 'all' default when key missing", () => {
    expect(loadLastInboxTab("co_1")).toBe("all");
  });

  test("returns 'all' default when stored value is not a known tab", () => {
    localStorage.setItem("paperclip:inbox:co_1:tab", "evil-tab");
    expect(loadLastInboxTab("co_1")).toBe("all");
  });

  test("returns 'all' default when localStorage.getItem throws", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("private mode");
    });
    expect(loadLastInboxTab("co_1")).toBe("all");
  });

  test("saveLastInboxTab does not throw on storage failure", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("quota exceeded");
    });
    expect(() => saveLastInboxTab("co_1", "recent")).not.toThrow();
  });
});

describe("loadReadInboxItems / saveReadInboxItems", () => {
  test("round-trips a Set as JSON array", () => {
    saveReadInboxItems("co_1", new Set(["iss_1", "iss_2"]));
    const ids = loadReadInboxItems("co_1");
    expect(ids).toBeInstanceOf(Set);
    expect(ids.has("iss_1")).toBe(true);
    expect(ids.has("iss_2")).toBe(true);
    expect(ids.has("iss_3")).toBe(false);
  });

  test("returns empty Set when key missing", () => {
    const ids = loadReadInboxItems("co_1");
    expect(ids).toBeInstanceOf(Set);
    expect(ids.size).toBe(0);
  });

  test("returns empty Set on JSON parse error", () => {
    localStorage.setItem("paperclip:inbox:co_1:read-items", "{not valid");
    expect(loadReadInboxItems("co_1").size).toBe(0);
  });

  test("returns empty Set when stored JSON is not an array", () => {
    localStorage.setItem("paperclip:inbox:co_1:read-items", JSON.stringify({ not: "array" }));
    expect(loadReadInboxItems("co_1").size).toBe(0);
  });

  test("filters out non-string entries", () => {
    localStorage.setItem(
      "paperclip:inbox:co_1:read-items",
      JSON.stringify(["ok", 42, null, "also_ok"]),
    );
    const ids = loadReadInboxItems("co_1");
    expect(ids.size).toBe(2);
    expect(ids.has("ok")).toBe(true);
    expect(ids.has("also_ok")).toBe(true);
  });

  test("returns empty Set when localStorage.getItem throws", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("private mode");
    });
    expect(loadReadInboxItems("co_1").size).toBe(0);
  });

  test("saveReadInboxItems does not throw on storage failure", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("quota exceeded");
    });
    expect(() => saveReadInboxItems("co_1", new Set(["iss_1"]))).not.toThrow();
  });

  test("namespaces by companyId — separate companies don't bleed state", () => {
    saveReadInboxItems("co_a", new Set(["iss_1"]));
    expect(loadReadInboxItems("co_b").size).toBe(0);
  });
});

describe("triage storage helpers", () => {
  test("ack-runs round-trips a Set and isolates per company", () => {
    saveAckRuns("co1", new Set(["r1", "r2"]));
    expect(loadAckRuns("co1")).toEqual(new Set(["r1", "r2"]));
    expect(loadAckRuns("co2")).toEqual(new Set()); // per-company isolation
  });

  test("ack-runs returns empty Set when missing or malformed", () => {
    expect(loadAckRuns("missing")).toEqual(new Set());
    localStorage.setItem("paperclip:inbox:bad:ack-runs", "{not json");
    expect(loadAckRuns("bad")).toEqual(new Set());
  });

  test("band-collapsed round-trips a boolean, default false", () => {
    expect(loadBandCollapsed("co1")).toBe(false);
    saveBandCollapsed("co1", true);
    expect(loadBandCollapsed("co1")).toBe(true);
    saveBandCollapsed("co1", false);
    expect(loadBandCollapsed("co1")).toBe(false);
  });
});
