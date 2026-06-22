import { describe, it, expect } from "vitest";
import { toMermaid, extractStepRefs } from "@/lib/lobster/toMermaid";
import type { LobsterWorkflow } from "@/lib/lobster/parseLobster";

describe("extractStepRefs", () => {
  const ids = new Set(["load-connected", "await-confirmation"]);
  it("keeps only $refs that match a known step id, stripping suffixes", () => {
    expect(extractStepRefs("$load-connected.json.gog x $action_type", ids)).toEqual([
      "load-connected",
    ]);
  });
  it("matches refs with hyphens and dedupes", () => {
    expect(
      extractStepRefs("$await-confirmation.json && $await-confirmation.stdout", ids),
    ).toEqual(["await-confirmation"]);
  });
  it("returns [] for undefined", () => {
    expect(extractStepRefs(undefined, ids)).toEqual([]);
  });
});

describe("toMermaid", () => {
  const wf: LobsterWorkflow = {
    name: "demo",
    steps: [
      { id: "load-connected", command: "openclaw.invoke --tool fast-io --action read", isApproval: false },
      {
        id: "load-gog",
        command: "openclaw.invoke --tool gog --action calendar-list-events",
        condition: "$load-connected.json.gog.connected == true",
        isApproval: false,
      },
      { id: "approve-send", command: "exec --shell 'true'", stdin: "$load-gog.json", isApproval: true },
    ],
  };

  it("starts with a flowchart TD header and one node per step", () => {
    const out = toMermaid(wf);
    expect(out.startsWith("flowchart TD")).toBe(true);
    expect(out).toContain('n_load_connected["load-connected');
    expect(out).toContain("gog · calendar-list-events"); // tool · action summary
  });

  it("renders approval steps as diamond nodes with the approval class", () => {
    const out = toMermaid(wf);
    expect(out).toContain("n_approve_send{");
    expect(out).toContain("classDef approval");
    expect(out).toMatch(/class .*n_approve_send.*approval;/);
  });

  it("draws a condition edge with a (truncated) label", () => {
    const out = toMermaid(wf);
    expect(out).toMatch(/n_load_connected -->\|".*gog\.connected.*"\| n_load_gog/);
  });

  it("draws a plain data edge for stdin refs and no self edges", () => {
    const out = toMermaid(wf);
    expect(out).toContain("n_load_gog --> n_approve_send");
    expect(out).not.toContain("n_load_gog --> n_load_gog");
  });

  it("draws data edges for $step refs passed through env", () => {
    const out = toMermaid({
      steps: [
        { id: "recon", command: "exec", isApproval: false },
        { id: "merge", command: "exec", env: '{"XERO":"$recon.stdout"}', isApproval: false },
      ],
    });
    expect(out).toContain("n_recon --> n_merge");
  });
});
