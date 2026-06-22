import { describe, it, expect } from "vitest";
import { lobsterToMermaid } from "@/lib/lobster";

const WF = `name: demo
steps:
  - id: load
    command: exec --shell 'true'
  - id: enrich
    command: exec
    stdin: $load.stdout
  - id: approve-send
    approval:
      prompt: "ok?"
    command: exec
    when: $enrich.approved
`;

describe("lobsterToMermaid", () => {
  it("returns ok + a flowchart for a valid workflow", () => {
    const res = lobsterToMermaid(WF);
    expect(res.ok).toBe(true);
    if (res.ok) expect(res.mermaid.startsWith("flowchart TD")).toBe(true);
  });

  // The output is produced by the vendored lobster emitter, so these assert
  // canonical `lobster graph` behavior rather than our own rules.
  it("emits canonical sequential, stdin, and when edges", () => {
    const res = lobsterToMermaid(WF);
    expect(res.ok).toBe(true);
    if (!res.ok) return;
    expect(res.mermaid).toContain("-->|next|");
    expect(res.mermaid).toContain("|stdin|");
    expect(res.mermaid).toContain("when: $enrich.approved");
  });

  it("renders approval steps as diamond nodes", () => {
    const res = lobsterToMermaid(WF);
    expect(res.ok).toBe(true);
    if (res.ok) expect(res.mermaid).toMatch(/approve_send\{/);
  });

  it("returns reason 'empty' when there are no steps", () => {
    expect(lobsterToMermaid("name: x\n")).toEqual({
      ok: false,
      reason: "empty",
      message: expect.any(String),
    });
  });

  it("returns reason 'parse' on invalid YAML", () => {
    const res = lobsterToMermaid(": : :");
    expect(res.ok).toBe(false);
    if (!res.ok) expect(res.reason).toBe("parse");
  });

  it("returns reason 'parse' for a non-mapping document", () => {
    const res = lobsterToMermaid("- a\n- b\n");
    expect(res.ok).toBe(false);
    if (!res.ok) expect(res.reason).toBe("parse");
  });
});
