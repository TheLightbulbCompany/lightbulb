import { describe, it, expect } from "vitest";
import type { CompanyAgent } from "@/components/teams/shared/types";
import { resolveAutomationRunner } from "../automationRunner";

function agent(over: Partial<CompanyAgent> = {}): CompanyAgent {
  return { id: "ag", name: "Agent", ...over };
}

describe("resolveAutomationRunner", () => {
  it("picks the rootless CEO", () => {
    const agents = [
      agent({ id: "vera", name: "Vera", role: "general" }),
      agent({ id: "friday", name: "Friday", role: "ceo", seeded: true }),
    ];
    expect(resolveAutomationRunner(agents)?.id).toBe("friday");
  });

  it("resolves a legacy company whose agents carry no seeded stamp", () => {
    // No persisted seeded_agent_ids (legacy company) or a failed lookup →
    // the BFF stamps every agent `seeded: false`. The backend's _find_friday_id
    // ignores that field entirely and still creates the automation for the
    // rootless CEO, so this resolver must too.
    const agents = [
      agent({ id: "friday", name: "Friday", role: "ceo", seeded: false }),
      agent({ id: "vera", name: "Vera", role: "general", seeded: false }),
    ];
    expect(resolveAutomationRunner(agents)?.id).toBe("friday");
  });

  it("resolves when the seeded field is absent altogether", () => {
    const agents = [agent({ id: "friday", name: "Friday", role: "ceo" })];
    expect(resolveAutomationRunner(agents)?.id).toBe("friday");
  });

  it("prefers the rootless CEO over a deployed package's division-head CEO", () => {
    // A company deploy grafts the package's own CEO under Friday, so 2+ agents
    // hold role "ceo" and list order is not contractually Friday-first.
    const agents = [
      agent({ id: "division", name: "Nova", role: "ceo", reportsTo: "friday" }),
      agent({ id: "friday", name: "Friday", role: "ceo" }),
    ];
    expect(resolveAutomationRunner(agents)?.id).toBe("friday");
  });

  it("falls back to the first CEO when none is rootless", () => {
    const agents = [
      agent({ id: "a", name: "A", role: "ceo", reportsTo: "x" }),
      agent({ id: "b", name: "B", role: "ceo", reportsTo: "y" }),
    ];
    expect(resolveAutomationRunner(agents)?.id).toBe("a");
  });

  it("treats an empty reportsTo as rootless", () => {
    const agents = [agent({ id: "friday", name: "Friday", role: "ceo", reportsTo: "" })];
    expect(resolveAutomationRunner(agents)?.id).toBe("friday");
  });

  it("returns undefined when the roster holds no CEO", () => {
    expect(resolveAutomationRunner([])).toBeUndefined();
    expect(
      resolveAutomationRunner([agent({ id: "vera", role: "general" })]),
    ).toBeUndefined();
  });
});
