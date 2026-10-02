import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const surfaces = readFileSync(
  resolve(__dirname, "../hermes/skills-src/lightbulb/references/surfaces.md"),
  "utf8",
);

describe("bundled Lightbulb surface guidance", () => {
  it("directs owners to Library on the workspace rail", () => {
    expect(surfaces).toContain("| Library | thelightbulb.company/chat (workspace rail)");
    expect(surfaces).not.toContain("Teams panel, Knowledge");
  });
});
