import { describe, it, expect } from "vitest";
import { lobsterToMermaid } from "@/lib/lobster";

describe("lobsterToMermaid", () => {
  it("returns ok + mermaid for a valid workflow", () => {
    const res = lobsterToMermaid(`name: x
steps:
  - id: a
    command: exec --shell 'true'
`);
    expect(res.ok).toBe(true);
    if (res.ok) expect(res.mermaid).toContain("flowchart TD");
  });

  it("returns reason 'empty' when there are no steps", () => {
    const res = lobsterToMermaid("name: x\n");
    expect(res).toEqual({ ok: false, reason: "empty", message: expect.any(String) });
  });

  it("returns reason 'parse' on invalid YAML", () => {
    const res = lobsterToMermaid(": : :");
    expect(res.ok).toBe(false);
    if (!res.ok) expect(res.reason).toBe("parse");
  });
});
