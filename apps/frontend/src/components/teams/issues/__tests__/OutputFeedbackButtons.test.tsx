import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { OutputFeedbackButtons } from "@/components/teams/issues/OutputFeedbackButtons";

describe("OutputFeedbackButtons", () => {
  it("renders the up/down vote buttons", () => {
    render(<OutputFeedbackButtons onVote={async () => {}} />);
    expect(screen.getByRole("button", { name: /helpful/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /needs work/i })).toBeInTheDocument();
  });

  it('casts an "up" vote when Helpful is clicked', async () => {
    const onVote = vi.fn().mockResolvedValue(undefined);
    render(<OutputFeedbackButtons onVote={onVote} />);
    fireEvent.click(screen.getByRole("button", { name: /helpful/i }));
    await waitFor(() => expect(onVote).toHaveBeenCalledWith("up", undefined));
  });

  it("highlights the active vote via aria-pressed", () => {
    render(<OutputFeedbackButtons activeVote="up" onVote={async () => {}} />);
    expect(screen.getByRole("button", { name: /helpful/i })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: /needs work/i })).toHaveAttribute("aria-pressed", "false");
  });

  it('casts "down" immediately and opens the reason note; Save note re-casts with the reason', async () => {
    const onVote = vi.fn().mockResolvedValue(undefined);
    render(<OutputFeedbackButtons onVote={onVote} />);

    fireEvent.click(screen.getByRole("button", { name: /needs work/i }));
    // Down vote registers immediately (no reason).
    await waitFor(() => expect(onVote).toHaveBeenCalledWith("down", undefined));

    // Reason collector appears.
    const textarea = await screen.findByPlaceholderText(/add a short note/i);
    fireEvent.change(textarea, { target: { value: "missed the ask" } });

    fireEvent.click(screen.getByRole("button", { name: /save note/i }));
    await waitFor(() =>
      expect(onVote).toHaveBeenLastCalledWith("down", { reason: "missed the ask" }),
    );
  });

  it("disables Save note until a reason is entered", async () => {
    render(<OutputFeedbackButtons onVote={async () => {}} />);
    fireEvent.click(screen.getByRole("button", { name: /needs work/i }));
    const save = await screen.findByRole("button", { name: /save note/i });
    expect(save).toBeDisabled();
  });
});
