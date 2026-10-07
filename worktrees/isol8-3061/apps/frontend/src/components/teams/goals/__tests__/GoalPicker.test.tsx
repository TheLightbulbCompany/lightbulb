import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { GoalPicker } from "../GoalPicker";

const GOALS = [
  { id: "g1", title: "Grow revenue" },
  { id: "g2", title: "Ship marketplace" },
  { id: "g3", title: "Reduce churn" },
];

beforeEach(() => {
  vi.clearAllMocks();
});

describe("GoalPicker", () => {
  it("shows 'No parent' when nothing is selected and the selected title otherwise", () => {
    const { rerender } = render(
      <GoalPicker goals={GOALS} selectedGoalId={null} onChange={vi.fn()} />,
    );
    expect(screen.getByRole("button", { name: /no parent/i })).toBeInTheDocument();

    rerender(
      <GoalPicker goals={GOALS} selectedGoalId="g2" onChange={vi.fn()} />,
    );
    expect(
      screen.getByRole("button", { name: /ship marketplace/i }),
    ).toBeInTheDocument();
  });

  it("selecting a goal in the popover fires onChange with its id", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<GoalPicker goals={GOALS} selectedGoalId={null} onChange={onChange} />);
    await user.click(screen.getByRole("button", { name: /no parent/i }));
    await user.click(await screen.findByText("Reduce churn"));
    expect(onChange).toHaveBeenCalledWith("g3");
  });

  it("the clear option fires onChange(null)", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<GoalPicker goals={GOALS} selectedGoalId="g1" onChange={onChange} />);
    // Trigger label shows the selected goal; the popover's "No parent" entry
    // (distinct from the trigger) clears it.
    await user.click(screen.getByRole("button", { name: /grow revenue/i }));
    const clearBtn = await screen.findByRole("button", { name: /^no parent$/i });
    await user.click(clearBtn);
    expect(onChange).toHaveBeenCalledWith(null);
  });

  it("excludeGoalId hides that goal so a goal can't be its own parent", async () => {
    const user = userEvent.setup();
    render(
      <GoalPicker
        goals={GOALS}
        selectedGoalId={null}
        onChange={vi.fn()}
        excludeGoalId="g2"
      />,
    );
    await user.click(screen.getByRole("button", { name: /no parent/i }));
    expect(await screen.findByText("Grow revenue")).toBeInTheDocument();
    expect(screen.queryByText("Ship marketplace")).not.toBeInTheDocument();
  });
});
