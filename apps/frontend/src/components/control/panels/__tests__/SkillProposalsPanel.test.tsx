import { render, screen, waitFor } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { SkillProposalsPanel } from "../SkillProposalsPanel";

vi.mock("@/hooks/useRuntimeRpc", () => ({
  useRuntimeRpc: (method: string | null) => {
    if (method === "skills.proposals.list") {
      return {
        data: { updatedAt: "t", proposals: [
          { id: "p1", kind: "create", status: "pending", title: "Add deploy skill", description: "d", skillName: "deploy", skillKey: "deploy", createdAt: "t", updatedAt: "t", scanState: "clean" },
        ] },
        error: undefined, isLoading: false, mutate: vi.fn(),
      };
    }
    return { data: undefined, error: undefined, isLoading: false, mutate: vi.fn() };
  },
  useRuntimeRpcMutation: () => vi.fn(),
}));

describe("SkillProposalsPanel", () => {
  it("lists pending proposals with title + status", async () => {
    render(<SkillProposalsPanel />);
    await waitFor(() => expect(screen.getByText("Add deploy skill")).toBeInTheDocument());
    expect(screen.getByText(/pending/i)).toBeInTheDocument();
  });
});
