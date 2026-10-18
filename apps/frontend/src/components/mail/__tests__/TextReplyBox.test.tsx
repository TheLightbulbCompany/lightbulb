import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

const mockPost = vi.fn();
vi.mock("@/lib/api", () => ({ useApi: () => ({ post: mockPost }) }));

import { TextReplyBox } from "../TextReplyBox";

describe("TextReplyBox", () => {
  beforeEach(() => mockPost.mockReset());

  it("send posts key and text, clears on sent, shows the status otherwise", async () => {
    mockPost.mockResolvedValueOnce({ status: "send_failed" }).mockResolvedValueOnce({ status: "sent" });
    render(<TextReplyBox messageKey="k9" humanThread={false} />);
    const box = screen.getByRole("textbox");
    fireEvent.change(box, { target: { value: "On my way" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    expect(await screen.findByText("send_failed")).toBeInTheDocument();
    expect(mockPost).toHaveBeenCalledWith("/agent-text/reply", { key: "k9", text: "On my way" });
    expect(box).toHaveValue("On my way");
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(box).toHaveValue(""));
  });

  it("toggle posts human then agent", async () => {
    mockPost.mockResolvedValue({ status: "ok" });
    const { rerender } = render(<TextReplyBox messageKey="k9" humanThread={false} />);
    fireEvent.click(screen.getByRole("button", { name: "Take over" }));
    await waitFor(() => expect(mockPost).toHaveBeenCalledWith("/agent-text/thread", { key: "k9", mode: "human" }));
    rerender(<TextReplyBox messageKey="k9" humanThread />);
    fireEvent.click(screen.getByRole("button", { name: "Hand back" }));
    await waitFor(() => expect(mockPost).toHaveBeenCalledWith("/agent-text/thread", { key: "k9", mode: "agent" }));
  });
});
