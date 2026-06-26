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

  // Real workflows embed multi-line `command: >` / `run: |` blocks. The vendored
  // emitter copies that preview verbatim into the `["..."]` node label and only
  // escapes quotes — so a raw newline from a folded/literal YAML scalar used to
  // split the node statement across lines, which makes mermaid.render() throw.
  // The label text must stay on a single line.
  const MULTILINE_WF = [
    "name: multiline",
    "steps:",
    "  - id: brief",
    "    run: |",
    "      node -e 'const a=1;",
    "      const b=2;'",
    "  - id: tail",
    "    command: exec",
    "    stdin: $brief.stdout",
  ].join("\n");

  it("keeps node labels single-line for multi-line commands", () => {
    const res = lobsterToMermaid(MULTILINE_WF);
    expect(res.ok).toBe(true);
    if (!res.ok) return;
    // Every node-definition line must open AND close its label on the same line;
    // a leaked newline would split `key["label` from `more"]`.
    for (const line of res.mermaid.split("\n")) {
      if (line.includes('["')) expect(line).toContain('"]');
      if (line.includes('{"')) expect(line).toContain('"}');
    }
    // And no raw newline should survive inside a node label.
    expect(/\["[^"\]\n]*\n/.test(res.mermaid)).toBe(false);
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
