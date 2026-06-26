import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { GetStartedChecklist } from "../GetStartedChecklist";

const allFalse = { meet: false, chat: false, task: false, hire: false, channel: false };

describe("GetStartedChecklist", () => {
  it("calls onAction with the step id when a CTA is clicked", async () => {
    const onAction = vi.fn();
    render(<GetStartedChecklist completed={allFalse} onAction={onAction} onMeet={vi.fn()} onDismiss={vi.fn()} />);
    await userEvent.click(screen.getByRole("button", { name: /browse marketplace/i }));
    expect(onAction).toHaveBeenCalledWith("hire");
  });
  it("renders the 'set during signup' confirmation row", () => {
    render(<GetStartedChecklist completed={allFalse} onAction={vi.fn()} onMeet={vi.fn()} onDismiss={vi.fn()} />);
    expect(screen.getByText(/set during signup/i)).toBeInTheDocument();
  });
});
