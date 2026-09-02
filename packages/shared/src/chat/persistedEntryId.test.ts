import { describe, it, expect } from "vitest";

import { persistedEntryId } from "./history";

describe("persistedEntryId", () => {
  it("prefers __openclaw.id — what chat.history actually stamps", () => {
    expect(
      persistedEntryId({
        role: "user",
        content: "hi",
        __openclaw: { id: "entry-1" },
        messageId: "surface-1",
        id: "surface-2",
      }),
    ).toBe("entry-1");
  });

  it("falls back to messageId when __openclaw carries no id", () => {
    expect(
      persistedEntryId({ role: "user", content: "hi", messageId: "surface-1", id: "surface-2" }),
    ).toBe("surface-1");
  });

  it("falls back to id when messageId is absent or blank", () => {
    expect(persistedEntryId({ role: "user", content: "hi", id: "surface-2" })).toBe("surface-2");
    expect(
      persistedEntryId({ role: "user", content: "hi", messageId: "   ", id: "surface-2" }),
    ).toBe("surface-2");
  });

  it("returns null for an optimistic local turn (no ids at all)", () => {
    expect(
      persistedEntryId({ role: "user", content: [{ type: "text", text: "hi" }], timestamp: 1 }),
    ).toBeNull();
  });

  it("returns null for a `pending:` queued-input placeholder row", () => {
    expect(persistedEntryId({ role: "user", content: "hi", __openclaw: { id: "pending:42" } })).toBeNull();
  });

  it("returns null for blank / non-string ids", () => {
    expect(persistedEntryId({ role: "user", content: "hi", id: "" })).toBeNull();
    expect(
      persistedEntryId({ role: "user", content: "hi", id: 7 } as unknown as Parameters<
        typeof persistedEntryId
      >[0]),
    ).toBeNull();
  });
});
