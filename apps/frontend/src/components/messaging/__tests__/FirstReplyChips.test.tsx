import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { FIRST_REPLIES, FirstReplyChips } from "../FirstReplyChips";

describe("FirstReplyChips", () => {
  it("offers the three pre-typed replies and prefills, never sends", async () => {
    const onPick = vi.fn();
    render(<FirstReplyChips onPick={onPick} />);
    const chips = screen.getAllByRole("button");
    expect(chips.map((c) => c.textContent)).toEqual([...FIRST_REPLIES].map((t) => t.trim()));
    await userEvent.click(chips[0]);
    expect(onPick).toHaveBeenCalledWith("Here's our website: ");
    await userEvent.click(chips[2]);
    expect(onPick).toHaveBeenLastCalledWith("Not sure yet. Pick something useful and show me how you work.");
  });
});
