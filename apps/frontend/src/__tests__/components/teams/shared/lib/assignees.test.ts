import { describe, test, expect } from "vitest";
import {
  assigneeValueFromSelection,
  parseAssigneeValue,
  currentUserAssigneeOption,
  formatAssigneeUserLabel,
} from "@/components/teams/shared/lib/assignees";

describe("formatAssigneeUserLabel", () => {
  test("returns null when userId is missing", () => {
    expect(formatAssigneeUserLabel(null, "u1")).toBeNull();
    expect(formatAssigneeUserLabel(undefined, "u1")).toBeNull();
  });

  test("returns 'You' when userId === currentUserId", () => {
    expect(formatAssigneeUserLabel("u1", "u1")).toBe("You");
  });

  test("returns Map-supplied label when present", () => {
    const labels = new Map([["u2", "Alice"]]);
    expect(formatAssigneeUserLabel("u2", "u1", labels)).toBe("Alice");
  });

  test("returns Record-supplied label when present", () => {
    expect(formatAssigneeUserLabel("u2", "u1", { u2: "Bob" })).toBe("Bob");
  });

  test("returns 'Board' for the local-board sentinel", () => {
    expect(formatAssigneeUserLabel("local-board", "u1")).toBe("Board");
  });

  test("returns null for an unresolvable userId (no id-prefix gibberish)", () => {
    expect(formatAssigneeUserLabel("user_abcdef123", "u1")).toBeNull();
  });
});

describe("assigneeValueFromSelection / parseAssigneeValue", () => {
  test("round-trips an agent selection", () => {
    const v = assigneeValueFromSelection({ assigneeAgentId: "ag1", assigneeUserId: null });
    expect(v).toBe("agent:ag1");
    expect(parseAssigneeValue(v)).toEqual({ assigneeAgentId: "ag1", assigneeUserId: null });
  });

  test("round-trips a user selection", () => {
    const v = assigneeValueFromSelection({ assigneeAgentId: null, assigneeUserId: "u1" });
    expect(v).toBe("user:u1");
    expect(parseAssigneeValue(v)).toEqual({ assigneeAgentId: null, assigneeUserId: "u1" });
  });

  test("parses empty value as fully null selection", () => {
    expect(parseAssigneeValue("")).toEqual({ assigneeAgentId: null, assigneeUserId: null });
  });
});

describe("currentUserAssigneeOption", () => {
  test("returns empty array when currentUserId is missing", () => {
    expect(currentUserAssigneeOption(null)).toEqual([]);
  });

  test("builds a 'Me' option keyed by user value", () => {
    const [opt] = currentUserAssigneeOption("u1");
    expect(opt).toEqual({
      id: "user:u1",
      label: "Me",
      searchText: "me human u1",
    });
  });
});
