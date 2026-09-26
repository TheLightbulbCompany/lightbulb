import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const ROOT = join(__dirname, "../../..");
const workflow = readFileSync(
  join(ROOT, ".github/workflows/mobile-release.yml"),
  "utf8",
);

describe("mobile release workflow", () => {
  it("hands an accepted TestFlight submission to EAS without waiting out the job timeout", () => {
    const submitStep =
      workflow.match(
        /- name: (?:Submit to|Schedule) TestFlight submission[\s\S]*?(?=\n\s{6}- name:|$)/,
      )?.[0] ?? "";

    expect(submitStep).toContain("--no-wait");
  });
});
