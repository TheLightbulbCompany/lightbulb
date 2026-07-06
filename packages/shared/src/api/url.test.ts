import { describe, expect, it } from "vitest";

import { deriveWebSocketUrl } from "./url";

// Ported from the web frontend's api.test.ts when the derivation moved here
// (Phase 1). The env-override behavior is now the platform wrapper's job —
// covered here as the explicit `override` argument.
describe("deriveWebSocketUrl", () => {
  it("rewrites prod apex hostname: api.isol8.co → ws.isol8.co", () => {
    expect(deriveWebSocketUrl("https://api.isol8.co/api/v1")).toBe(
      "wss://ws.isol8.co",
    );
  });

  it("rewrites dev env-prefixed hostname: api-dev.isol8.co → ws-dev.isol8.co", () => {
    expect(deriveWebSocketUrl("https://api-dev.isol8.co/api/v1")).toBe(
      "wss://ws-dev.isol8.co",
    );
  });

  it("rewrites staging env-prefixed hostname", () => {
    expect(deriveWebSocketUrl("https://api-staging.isol8.co/api/v1")).toBe(
      "wss://ws-staging.isol8.co",
    );
  });

  it("leaves localhost untouched and strips /api/v1", () => {
    expect(deriveWebSocketUrl("http://localhost:8000/api/v1")).toBe(
      "ws://localhost:8000",
    );
  });

  it("downgrades https→wss and http→ws", () => {
    expect(deriveWebSocketUrl("http://api-dev.isol8.co/api/v1")).toBe(
      "ws://ws-dev.isol8.co",
    );
  });

  it("override short-circuits derivation", () => {
    expect(
      deriveWebSocketUrl("https://api.isol8.co/api/v1", "wss://custom.example.com"),
    ).toBe("wss://custom.example.com");
  });

  it("empty/undefined override falls through to derivation", () => {
    expect(deriveWebSocketUrl("https://api.isol8.co/api/v1", "")).toBe(
      "wss://ws.isol8.co",
    );
    expect(deriveWebSocketUrl("https://api.isol8.co/api/v1", null)).toBe(
      "wss://ws.isol8.co",
    );
  });

  it('does not rewrite an unrelated hostname that happens to start with "api"', () => {
    expect(deriveWebSocketUrl("https://apiary.com/api/v1")).toBe(
      "wss://apiary.com",
    );
  });
});
