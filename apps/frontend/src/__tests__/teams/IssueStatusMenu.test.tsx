import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

const update = vi.fn().mockResolvedValue({});
vi.mock("@/components/teams/issues/hooks/useIssueMutations", () => ({
  useIssueMutations: () => ({ update, create: vi.fn(), createChild: vi.fn(), addComment: vi.fn() }),
}));

import { IssueStatusMenu } from "@/components/teams/issues/IssueStatusMenu";

function issue(status: string) {
  return { id: "i1", title: "T", status } as never;
}

beforeEach(() => update.mockClear());

describe("IssueStatusMenu", () => {
  it("offers Close + Cancel (not Reopen) for an open issue, and closes it", async () => {
    const user = userEvent.setup();
    render(<IssueStatusMenu issue={issue("in_progress")} />);
    await user.click(screen.getByRole("button", { name: /issue actions/i }));
    expect(await screen.findByText("Close")).toBeInTheDocument();
    expect(screen.getByText("Cancel")).toBeInTheDocument();
    expect(screen.queryByText("Reopen")).toBeNull();
    await user.click(screen.getByText("Close"));
    expect(update).toHaveBeenCalledWith("i1", { status: "done" });
  });

  it("cancels an open issue", async () => {
    const user = userEvent.setup();
    render(<IssueStatusMenu issue={issue("todo")} />);
    await user.click(screen.getByRole("button", { name: /issue actions/i }));
    await user.click(await screen.findByText("Cancel"));
    expect(update).toHaveBeenCalledWith("i1", { status: "cancelled" });
  });

  it("offers only Reopen for a resolved issue, and fires onChanged", async () => {
    const user = userEvent.setup();
    const onChanged = vi.fn();
    render(<IssueStatusMenu issue={issue("done")} onChanged={onChanged} />);
    await user.click(screen.getByRole("button", { name: /issue actions/i }));
    expect(await screen.findByText("Reopen")).toBeInTheDocument();
    expect(screen.queryByText("Close")).toBeNull();
    await user.click(screen.getByText("Reopen"));
    expect(update).toHaveBeenCalledWith("i1", { status: "todo" });
    await vi.waitFor(() => expect(onChanged).toHaveBeenCalled());
  });
});
