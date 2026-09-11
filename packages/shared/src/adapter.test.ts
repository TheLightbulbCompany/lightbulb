import { describe, expect, it } from "vitest";

import type { PlatformAdapter } from "./adapter";

// Harness smoke test: proves the package's vitest + TS wiring works and the
// adapter contract is implementable without any platform SDK. Real logic
// tests arrive with the Phase 1 extraction.
describe("PlatformAdapter contract", () => {
  it("is implementable with plain values", async () => {
    const adapter: PlatformAdapter = {
      getToken: async () => "token",
      apiUrl: "https://api-dev.isol8.co/api/v1",
      uuid: () => "3f2504e0-4f89-41d3-9a0c-0305e82c3301",
    };
    expect(await adapter.getToken()).toBe("token");
    expect(adapter.uuid()).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/,
    );
  });
});
