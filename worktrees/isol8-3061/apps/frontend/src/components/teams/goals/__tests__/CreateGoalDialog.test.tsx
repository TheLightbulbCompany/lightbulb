// Goals lost their tab to Tasks; this pins that creating one is still possible.
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { CreateGoalDialog } from "@/components/teams/goals/CreateGoalDialog";

vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({ post: vi.fn(), read: () => ({ data: undefined }) }),
}));

describe("CreateGoalDialog survives the Goals tab", () => {
  it("renders the create form outside GoalsPanel", () => {
    render(<CreateGoalDialog goals={[]} onClose={() => {}} onCreated={() => {}} />);
    expect(screen.getByText("New goal")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /create/i })).toBeDisabled();
  });
});
