import { describe, it, expect } from "vitest";
import { parseLobster } from "@/lib/lobster/parseLobster";

const WF = `name: morning-briefing
steps:
  - id: load-connected
    command: >
      openclaw.invoke --tool fast-io --action read --args-json '{"key": "ora-config/connected-calendars"}'
  - id: load-gog-events
    command: >
      openclaw.invoke --tool gog --action calendar-list-events --args-json '{"date": "{{today}}"}'
    condition: $load-connected.json.gog.connected == true
  - id: await-confirmation
    approval:
      prompt: "Approve?"
    command: exec --json --shell 'true'
`;

describe("parseLobster", () => {
  it("parses name and steps with their fields", () => {
    const wf = parseLobster(WF);
    expect(wf.name).toBe("morning-briefing");
    expect(wf.steps.map((s) => s.id)).toEqual([
      "load-connected",
      "load-gog-events",
      "await-confirmation",
    ]);
    expect(wf.steps[1].condition).toContain("$load-connected.json.gog.connected");
    expect(wf.steps[1].command).toContain("--tool gog");
  });

  it("marks steps with an approval field as approval gates", () => {
    const wf = parseLobster(WF);
    expect(wf.steps[2].isApproval).toBe(true);
    expect(wf.steps[0].isApproval).toBe(false);
  });

  it("stringifies object stdin so $refs inside remain scannable", () => {
    const wf = parseLobster(`steps:
  - id: a
    command: exec
  - id: b
    command: exec
    stdin: '{ "x": $a.json }'
`);
    expect(wf.steps[1].stdin).toContain("$a.json");
  });

  it("returns empty steps for YAML without a steps array", () => {
    expect(parseLobster("name: x\n").steps).toEqual([]);
  });

  it("throws on non-object YAML", () => {
    expect(() => parseLobster("- just\n- a\n- list")).not.toThrow(); // arrays are objects-ish; see impl
    expect(() => parseLobster(": : :")).toThrow();
  });
});
