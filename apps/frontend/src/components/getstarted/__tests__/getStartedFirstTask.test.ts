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

  it("resolves the seeded CEO by role", () => {
    const agents: CompanyAgent[] = [
      agent({ id: "friday", name: "Friday", role: "ceo", seeded: true }),
      agent({ id: "atlas", name: "Atlas", role: "general", seeded: true }),
    ];
    expect(resolveCeoAgentId(agents)).toBe("friday");
  });

  it("does not infer the CEO from a seeded agent's name without role ceo", () => {
    const agents: CompanyAgent[] = [
      agent({ id: "friday", name: "Friday", seeded: true }),
    ];
    expect(resolveCeoAgentId(agents)).toBeUndefined();
  });

  it("ignores a user-created agent named Friday / using role ceo", () => {
    const agents: CompanyAgent[] = [
      agent({ id: "imposter", name: "Friday", role: "ceo", seeded: false }),
    ];
    expect(resolveCeoAgentId(agents)).toBeUndefined();
  });

  it("returns undefined when no seeded CEO exists", () => {
    expect(resolveCeoAgentId([])).toBeUndefined();
  });
});
