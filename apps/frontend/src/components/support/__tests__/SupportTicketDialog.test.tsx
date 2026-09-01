import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import React from "react";

import { SupportTicketDialog } from "../SupportTicketDialog";

// The dialog's only real dependency is the authenticated POST. next/navigation
// and @clerk/nextjs are already mocked globally in tests/setup.ts.
const post = vi.fn();
vi.mock("@/lib/api", () => ({
  useApi: () => ({ post }),
}));
vi.mock("@/lib/requestIdLog", () => ({ getRecentRequestIds: () => [] }));
vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));

beforeEach(() => {
  post.mockReset();
  post.mockResolvedValue({ status: "sent" });
});

describe("SupportTicketDialog", () => {
  it("prefills the message textarea from presetMessage and submits it", async () => {
    render(
      <SupportTicketDialog
        open
        onClose={vi.fn()}
        presetCategory="Integration request"
        presetMessage="I'd like to use Instagram."
      />,
    );

    // The Request access affordance is only useful if the prefill actually
    // lands in the field the user submits — assert the value, not the prop.
    const textarea = screen.getByLabelText("Message") as HTMLTextAreaElement;
    expect(textarea.value).toBe("I'd like to use Instagram.");
    expect((screen.getByLabelText("Topic") as HTMLSelectElement).value).toBe("Integration request");

    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    await waitFor(() => expect(post).toHaveBeenCalled());
    expect(post.mock.calls[0][1]).toMatchObject({
      message: "I'd like to use Instagram.",
      category: "Integration request",
    });
  });

  it("does not promise delivery or a record when the email did not go out", async () => {
    post.mockResolvedValue({ status: "received" });
    render(<SupportTicketDialog open onClose={vi.fn()} presetMessage="hello" />);

    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    // Must not promise a reply, and must not claim we hold the message — the
    // email IS the capture path, so the only honest fallback is a direct address.
    await waitFor(() =>
      expect(screen.getByText(/support@thelightbulb\.company/i)).toBeInTheDocument(),
    );
    expect(screen.queryByText(/on its way to our team/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/we logged your message/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/thanks — we got it/i)).not.toBeInTheDocument();
  });

  it("confirms delivery when the email actually went out", async () => {
    render(<SupportTicketDialog open onClose={vi.fn()} presetMessage="hello" />);

    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    await waitFor(() => expect(screen.getByText(/on its way to our team/i)).toBeInTheDocument());
  });
});
