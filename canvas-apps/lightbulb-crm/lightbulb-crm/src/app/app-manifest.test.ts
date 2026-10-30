import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

describe("Canvas publishing bridge", () => {
  it("publishes a room-scoped internal tool without an unnecessary data service", () => {
    const manifest = readFileSync(join(process.cwd(), "app.yaml"), "utf8");

    expect(manifest).toContain("kind: internal-tool");
    expect(manifest).toContain("open_to: room");
    expect(manifest).toContain('services: ["vercel/project"]');
    expect(manifest).not.toContain("neon/postgres");
  });
});
