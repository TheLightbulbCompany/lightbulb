// @vitest-environment node
import { describe, expect, it } from "vitest";

import nextConfig from "../../next.config";

describe("/install", () => {
  it("redirects to the engine installer", async () => {
    const redirects = await nextConfig.redirects!();
    for (const source of ["/install", "/install.sh"]) {
      expect(redirects).toContainEqual({
        source,
        destination: "https://github.com/TheLightbulbCompany/cli/releases/latest/download/install.sh",
        permanent: false,
      });
    }
  });
});
