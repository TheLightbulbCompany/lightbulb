// @vitest-environment node
import { describe, expect, it } from "vitest";

import nextConfig from "../../next.config";

// Spark's permanent public addresses. /spark/appcast.xml is baked into every
// shipped copy of the app as its update feed: that source path must NEVER change.
describe("Spark redirects", () => {
  it("send the download and the update feed to the spark release", async () => {
    const redirects = await nextConfig.redirects!();
    expect(redirects).toContainEqual({
      source: "/download/spark",
      destination: "https://github.com/TheLightbulbCompany/lightbulb/releases/download/spark/Spark.dmg",
      permanent: false,
    });
    expect(redirects).toContainEqual({
      source: "/spark/appcast.xml",
      destination: "https://github.com/TheLightbulbCompany/lightbulb/releases/download/spark/appcast.xml",
      permanent: false,
    });
  });

  it("send the Windows downloads to the spark-windows release", async () => {
    const redirects = await nextConfig.redirects!();
    expect(redirects).toContainEqual({
      source: "/download/spark-windows",
      destination: "https://github.com/TheLightbulbCompany/lightbulb/releases/download/spark-windows/Spark-Setup-x64.exe",
      permanent: false,
    });
    expect(redirects).toContainEqual({
      source: "/download/spark-windows-arm64",
      destination: "https://github.com/TheLightbulbCompany/lightbulb/releases/download/spark-windows/Spark-Setup-arm64.exe",
      permanent: false,
    });
  });
});
