import { describe, it, expect } from "vitest";
import type { CompanyAgent } from "@/components/teams/shared/types";
import {
  ONBOARDING_FIRST_TASK_TITLE,
  ONBOARDING_FIRST_TASK_DESCRIPTION,
  resolveCeoAgentId,
} from "../getStartedFirstTask";

function agent(over: Partial<CompanyAgent> = {}): CompanyAgent {
  return { id: "ag", name: "Agent", ...over };
}

describe("getStartedFirstTask", () => {
  it("copy is non-empty and carries the replace-me token", () => {
    expect(ONBOARDING_FIRST_TASK_TITLE).toContain("[your goal]");
    expect(ONBOARDING_FIRST_TASK_DESCRIPTION).toContain("[your goal]");
    expect(ONBOARDING_FIRST_TASK_DESCRIPTION.length).toBeGreaterThan(20);
  });

  it("resolves the root starter CEO by backend origin and role", () => {
    const agents: CompanyAgent[] = [
      agent({ id: "division", name: "Division lead", role: "ceo", origin: "Starter", reportsTo: "ceo" }),
      agent({ id: "ceo", name: "Renamed CEO", role: "ceo", origin: "Starter", reportsTo: null }),
    ];
    expect(resolveCeoAgentId(agents)).toBe("ceo");
  });

  it("accepts a legacy Seed root CEO", () => {
    const agents: CompanyAgent[] = [
      agent({ id: "legacy", name: "Renamed legacy CEO", role: "ceo", origin: "Seed" }),
    ];
    expect(resolveCeoAgentId(agents)).toBe("legacy");
  });

  it("ignores a custom root CEO regardless of its display name", () => {
    const agents: CompanyAgent[] = [
      agent({ id: "imposter", name: "Friday", role: "ceo", origin: "Custom" }),
    ];
    expect(resolveCeoAgentId(agents)).toBeUndefined();
  });

  it("returns undefined when no starter or legacy root CEO exists", () => {
    expect(resolveCeoAgentId([])).toBeUndefined();
  });
});
