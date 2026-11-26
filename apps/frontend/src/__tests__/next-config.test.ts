import { afterEach, describe, expect, it, vi } from "vitest";

async function loadConfig() {
  vi.resetModules();
  return (await import("../../next.config")).default;
}

async function afterFiles() {
  const rewrites = await (await loadConfig()).rewrites?.();
  return !Array.isArray(rewrites) ? rewrites?.afterFiles : undefined;
}

describe("same-origin API rewrite", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it("proxies only /api/v1 to API_ORIGIN", async () => {
    vi.stubEnv("API_ORIGIN", "https://api.thelightbulb.company/");
    expect(await afterFiles()).toContainEqual({
      source: "/api/v1/:path*",
      destination: "https://api.thelightbulb.company/api/v1/:path*",
    });
  });

  it("defaults to the dev API off Vercel", async () => {
    vi.stubEnv("API_ORIGIN", "");
    vi.stubEnv("VERCEL", "");
    expect(await afterFiles()).toContainEqual({
      source: "/api/v1/:path*",
      destination: "https://api-dev.thelightbulb.company/api/v1/:path*",
    });
  });

  it("fails the build on Vercel when API_ORIGIN is unset", async () => {
    vi.stubEnv("API_ORIGIN", "");
    vi.stubEnv("VERCEL", "1");
    await expect(loadConfig()).rejects.toThrow("API_ORIGIN");
  });
});
