import { describe, expect, it } from "vitest";

import { asAuditEntry } from "../narrow";

describe("asAuditEntry", () => {
  it("keeps a partial_failure result instead of reading it as success", () => {
    const raw = { timestamp_action_id: "t#1", result: "partial_failure" };
    expect(asAuditEntry(raw as never).result).toBe("partial_failure");
  });
});
