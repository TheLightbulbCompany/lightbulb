import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { NewIssueDialog } from "../NewIssueDialog";
import type {
  Issue,
  IssueCreateInput,
} from "@/components/teams/shared/types";

// Capture the payload the dialog hands to create() so we can assert the
// composer forwards the chosen status + work mode (not just title/priority).
const create = vi.fn<(input: IssueCreateInput) => Promise<Issue>>();
const createChild = vi.fn<(parentId: string, input: IssueCreateInput) => Promise<Issue>>();

vi.mock("../hooks/useIssueMutations", () => ({
  useIssueMutations: () => ({
    create,
    createChild,
    update: vi.fn(),
    remove: vi.fn(),
    addComment: vi.fn(),
  }),
}));

beforeEach(() => {
  vi.clearAllMocks();
  create.mockResolvedValue({ id: "iss_new", title: "T", status: "backlog" } as Issue);
});

describe("NewIssueDialog composer", () => {
  it("titles the dialog 'New task' and the primary button 'Create task'", () => {
    render(<NewIssueDialog open onOpenChange={vi.fn()} />);
    expect(screen.getByRole("heading", { name: "New task" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create task" })).toBeInTheDocument();
  });

  it("includes the chosen status + work mode in the create payload", async () => {
    const user = userEvent.setup();
    render(<NewIssueDialog open onOpenChange={vi.fn()} />);

    await user.type(screen.getByPlaceholderText("Task title"), "Wire the composer");

    // Status preset → Todo (native <select>).
    await user.selectOptions(screen.getByLabelText("Status"), "todo");

    // Work mode segmented control → Planning (radiogroup of pill buttons).
    await user.click(screen.getByRole("radio", { name: "Planning" }));

    await user.click(screen.getByRole("button", { name: "Create task" }));

    expect(create).toHaveBeenCalledTimes(1);
    expect(create).toHaveBeenCalledWith(
      expect.objectContaining({
        title: "Wire the composer",
        status: "todo",
        workMode: "planning",
      }),
    );
  });

  it("defaults to backlog/standard when the presets are left untouched", async () => {
    const user = userEvent.setup();
    render(<NewIssueDialog open onOpenChange={vi.fn()} />);

    await user.type(screen.getByPlaceholderText("Task title"), "Default task");
    await user.click(screen.getByRole("button", { name: "Create task" }));

    expect(create).toHaveBeenCalledWith(
      expect.objectContaining({ status: "backlog", workMode: "standard" }),
    );
  });
});
