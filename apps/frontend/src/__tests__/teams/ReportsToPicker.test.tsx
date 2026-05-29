// apps/frontend/src/__tests__/teams/ReportsToPicker.test.tsx
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import type { CompanyAgent } from "@/components/teams/shared/types";
import { ReportsToPicker, candidateManagers } from "@/components/teams/agents/ReportsToPicker";

const AGENTS: CompanyAgent[] = [
  { id: "a", name: "Friday", reportsTo: null },
  { id: "b", name: "Bob", reportsTo: "a" },
  { id: "c", name: "Cara", reportsTo: "b" },
];

describe("candidateManagers", () => {
  it("excludes self and descendants (cycle prevention)", () => {
    // For Bob (b), candidates exclude b itself and its descendant c.
    const ids = candidateManagers(AGENTS, "b").map((a) => a.id);
    expect(ids).toEqual(["a"]);
  });
});

describe("ReportsToPicker", () => {
  it("renders No manager + candidates and calls onChange", () => {
    const onChange = vi.fn();
    render(<ReportsToPicker agents={AGENTS} agentId="b" value="a" onChange={onChange} />);
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "" } });
    expect(onChange).toHaveBeenCalledWith(null);
  });
});
