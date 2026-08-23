// The acknowledgement beat on a resolved approval. It is decoration over a
// state change that already happened, so the tests that matter are the ones
// pinning that it stays decoration: it must never fire on a failure, and it
// must never gate the mutation.
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

// vi.mock factories are hoisted above every const in the file, so the mocks
// have to own their own fns via vi.hoisted rather than closing over locals.
const h = vi.hoisted(() => ({
  approve: vi.fn(),
  reject: vi.fn(),
  notifyApiError: vi.fn(),
}));

vi.mock("@/components/teams/inbox/hooks/useApprovalMutations", () => ({
  useApprovalMutations: () => ({ approve: h.approve, reject: h.reject }),
}));
vi.mock("@/lib/notify", () => ({ notifyApiError: h.notifyApiError }));

const { approve, reject, notifyApiError } = h;
vi.mock("@/components/teams/shell", () => ({
  EditorialButton: ({ children, ...rest }: React.ComponentProps<"button">) => (
    <button {...rest}>{children}</button>
  ),
}));

import { ApprovalRow } from "../ApprovalRow";

const approval = {
  id: "ap_1",
  type: "hire_agent",
  status: "pending",
  payload: {},
  createdAt: new Date().toISOString(),
} as never;

beforeEach(() => {
  approve.mockReset().mockResolvedValue(undefined);
  reject.mockReset();
  notifyApiError.mockReset();
});

describe("ApprovalRow completion beat", () => {
  it("acknowledges a successful approve", async () => {
    const { container } = render(<ApprovalRow approval={approval} />);
    fireEvent.click(screen.getByText("Approve"));

    await waitFor(() => {
      expect(container.querySelector("li")?.className).toMatch(/ws-settling/);
    });
    expect(container.querySelector(".ws-tick")).toBeTruthy();
  });

  it("does NOT acknowledge when the approve fails", async () => {
    // The failure mode that matters: flashing a confirmation for something
    // that did not happen is worse than no feedback at all.
    approve.mockRejectedValue(new Error("nope"));
    const { container } = render(<ApprovalRow approval={approval} />);
    fireEvent.click(screen.getByText("Approve"));

    await waitFor(() => expect(notifyApiError).toHaveBeenCalled());
    expect(container.querySelector("li")?.className).not.toMatch(/ws-settling/);
    expect(container.querySelector(".ws-tick")).toBeNull();
  });

  it("still calls the mutation exactly once", () => {
    // The beat is layered over the existing call path, not woven into it.
    render(<ApprovalRow approval={approval} />);
    fireEvent.click(screen.getByText("Approve"));
    expect(approve).toHaveBeenCalledTimes(1);
    expect(approve).toHaveBeenCalledWith("ap_1");
  });

  it("leaves reject untouched — only the affirmative action is celebrated", () => {
    render(<ApprovalRow approval={approval} />);
    fireEvent.click(screen.getByText("Reject"));
    expect(approve).not.toHaveBeenCalled();
  });
});
