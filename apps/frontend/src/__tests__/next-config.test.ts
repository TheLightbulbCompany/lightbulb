import { afterEach, describe, expect, it, vi } from "vitest";

import nextConfig from "../../next.config";

describe("local development API proxy", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it("only adds the dev API rewrite behind the explicit development flag", async () => {
    vi.stubEnv("NODE_ENV", "development");
    vi.stubEnv("LOCAL_DEV_API_PROXY", "1");
    const enabled = await nextConfig.rewrites?.();
    const enabledAfterFiles = !Array.isArray(enabled) ? enabled?.afterFiles : undefined;
    expect(enabledAfterFiles).toContainEqual({
      source: "/api/v1/:path*",
      destination: "https://api-dev.thelightbulb.company/api/v1/:path*",
    });

    vi.stubEnv("LOCAL_DEV_API_PROXY", "");
    const disabled = await nextConfig.rewrites?.();
    const disabledAfterFiles = !Array.isArray(disabled) ? disabled?.afterFiles : undefined;
    expect(disabledAfterFiles).not.toContainEqual({
      source: "/api/v1/:path*",
      destination: "https://api-dev.thelightbulb.company/api/v1/:path*",
    });

    vi.stubEnv("LOCAL_DEV_API_PROXY", "1");
    vi.stubEnv("NODE_ENV", "production");
    const production = await nextConfig.rewrites?.();
    const productionAfterFiles = !Array.isArray(production) ? production?.afterFiles : undefined;
    expect(productionAfterFiles).not.toContainEqual({
      source: "/api/v1/:path*",
      destination: "https://api-dev.thelightbulb.company/api/v1/:path*",
    });
  });
});
