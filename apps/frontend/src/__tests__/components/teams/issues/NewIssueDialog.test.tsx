import { describe, test, expect, vi, beforeEach } from "vitest";
import { render, fireEvent, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

vi.mock("@/components/teams/issues/hooks/useIssueMutations", () => ({
  useIssueMutations: vi.fn(),
}));

import { NewIssueDialog } from "@/components/teams/issues/NewIssueDialog";
import { useIssueMutations } from "@/components/teams/issues/hooks/useIssueMutations";
import type { CompanyAgent, IssueProject } from "@/components/teams/shared/types";

const mockCreate = vi.fn();

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(useIssueMutations).mockReturnValue({
    create: mockCreate,
    createChild: vi.fn(),
    update: vi.fn(),
    addComment: vi.fn(),
    remove: vi.fn(),
    monitorCheckNow: vi.fn(),
  });
  mockCreate.mockResolvedValue({ id: "iss_new" });
});

const baseProps = {
  open: true,
  onOpenChange: vi.fn(),
};

describe("NewIssueDialog", () => {
  test("renders dialog with title input and Create disabled when title empty", () => {
    render(<NewIssueDialog {...baseProps} />);
    expect(screen.getByPlaceholderText("Task title")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /create task/i })).toBeDisabled();
  });

  test("titles the dialog 'New task' and the primary button 'Create task'", () => {
    render(<NewIssueDialog {...baseProps} />);
    expect(screen.getByRole("heading", { name: "New task" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create task" })).toBeInTheDocument();
  });

  test("Create enabled once title is non-empty", () => {
    render(<NewIssueDialog {...baseProps} />);
    fireEvent.change(screen.getByPlaceholderText("Task title"), {
      target: { value: "Fix" },
    });
    expect(screen.getByRole("button", { name: /create task/i })).not.toBeDisabled();
  });

  test("submit fires create with trimmed inputs + priority default", async () => {
    render(<NewIssueDialog {...baseProps} />);
    fireEvent.change(screen.getByPlaceholderText("Task title"), {
      target: { value: "  Fix bug  " },
    });
    fireEvent.change(screen.getByPlaceholderText("Optional details..."), {
      target: { value: "Details" },
    });
    fireEvent.click(screen.getByRole("button", { name: /create task/i }));
    await waitFor(() =>
      expect(mockCreate).toHaveBeenCalledWith(
        expect.objectContaining({
          title: "Fix bug",
          description: "Details",
          priority: "medium",
        }),
      ),
    );
  });

  test("includes the chosen status + work mode in the create payload", async () => {
    const user = userEvent.setup();
    render(<NewIssueDialog {...baseProps} />);
    await user.type(screen.getByPlaceholderText("Task title"), "Wire the composer");
    // Status preset → Todo (native <select>).
    await user.selectOptions(screen.getByLabelText("Status"), "todo");
    // Work mode segmented control → Planning (radiogroup of pill buttons).
    await user.click(screen.getByRole("radio", { name: "Planning" }));
    await user.click(screen.getByRole("button", { name: "Create task" }));

    expect(mockCreate).toHaveBeenCalledWith(
      expect.objectContaining({
        title: "Wire the composer",
        status: "todo",
        workMode: "planning",
      }),
    );
  });

  test("defaults to backlog/standard when the presets are left untouched", async () => {
    const user = userEvent.setup();
    render(<NewIssueDialog {...baseProps} />);
    await user.type(screen.getByPlaceholderText("Task title"), "Default task");
    await user.click(screen.getByRole("button", { name: "Create task" }));
    expect(mockCreate).toHaveBeenCalledWith(
      expect.objectContaining({ status: "backlog", workMode: "standard" }),
    );
  });

  test("onCreated fires with the created issue id", async () => {
    const onCreated = vi.fn();
    render(<NewIssueDialog {...baseProps} onCreated={onCreated} />);
    fireEvent.change(screen.getByPlaceholderText("Task title"), {
      target: { value: "Fix" },
    });
    fireEvent.click(screen.getByRole("button", { name: /create task/i }));
    await waitFor(() => expect(onCreated).toHaveBeenCalledWith("iss_new"));
  });

  test("onOpenChange(false) called after successful create", async () => {
    const onOpenChange = vi.fn();
    render(<NewIssueDialog open onOpenChange={onOpenChange} />);
    fireEvent.change(screen.getByPlaceholderText("Task title"), {
      target: { value: "Fix" },
    });
    fireEvent.click(screen.getByRole("button", { name: /create task/i }));
    await waitFor(() => expect(onOpenChange).toHaveBeenCalledWith(false));
  });

  test("error from create renders in alert", async () => {
    mockCreate.mockRejectedValue(new Error("server boom"));
    render(<NewIssueDialog {...baseProps} />);
    fireEvent.change(screen.getByPlaceholderText("Task title"), {
      target: { value: "Fix" },
    });
    fireEvent.click(screen.getByRole("button", { name: /create task/i }));
    await waitFor(() => expect(screen.getByRole("alert")).toBeInTheDocument());
    expect(screen.getByRole("alert")).toHaveTextContent(/server boom/);
  });

  test("project select renders only when projects provided", () => {
    const projects: IssueProject[] = [{ id: "p1", name: "Inbox v1" }];
    render(<NewIssueDialog {...baseProps} projects={projects} />);
    expect(screen.getByLabelText(/project/i)).toBeInTheDocument();
    expect(screen.getByText("Inbox v1")).toBeInTheDocument();
  });

  test("agent assignee select renders only when agents provided", () => {
    const agents: CompanyAgent[] = [{ id: "ag_1", name: "Main Agent" }];
    render(<NewIssueDialog {...baseProps} agents={agents} />);
    expect(screen.getByLabelText(/assign to agent/i)).toBeInTheDocument();
    expect(screen.getByText("Main Agent")).toBeInTheDocument();
  });

  test("submit fires create with selected project + assignee", async () => {
    const projects: IssueProject[] = [{ id: "p1", name: "Inbox v1" }];
    const agents: CompanyAgent[] = [{ id: "ag_1", name: "Main Agent" }];
    render(<NewIssueDialog {...baseProps} projects={projects} agents={agents} />);
    fireEvent.change(screen.getByPlaceholderText("Task title"), {
      target: { value: "Fix" },
    });
    fireEvent.change(screen.getByLabelText(/project/i), {
      target: { value: "p1" },
    });
    fireEvent.change(screen.getByLabelText(/assign to agent/i), {
      target: { value: "ag_1" },
    });
    fireEvent.click(screen.getByRole("button", { name: /create task/i }));
    await waitFor(() =>
      expect(mockCreate).toHaveBeenCalledWith(
        expect.objectContaining({
          title: "Fix",
          projectId: "p1",
          assigneeAgentId: "ag_1",
        }),
      ),
    );
  });

  test("seeds Title/Description/assignee from default props", () => {
    const agents: CompanyAgent[] = [
      { id: "friday", name: "Friday", role: "ceo", seeded: true },
    ];
    render(
      <NewIssueDialog
        {...baseProps}
        agents={agents}
        defaultTitle="Build out my team for [your goal]"
        defaultDescription="You are my CEO."
        defaultAssigneeAgentId="friday"
      />,
    );
    expect(screen.getByPlaceholderText("Task title")).toHaveValue(
      "Build out my team for [your goal]",
    );
    expect(screen.getByPlaceholderText("Optional details...")).toHaveValue(
      "You are my CEO.",
    );
    expect(screen.getByLabelText(/assign to agent/i)).toHaveValue("friday");
  });

  test("stays blank when no default props are given", () => {
    render(<NewIssueDialog {...baseProps} />);
    expect(screen.getByPlaceholderText("Task title")).toHaveValue("");
    expect(screen.getByPlaceholderText("Optional details...")).toHaveValue("");
  });

  test("seeds the assignee when the default arrives late, preserving edits", async () => {
    const user = userEvent.setup();
    const friday: CompanyAgent = {
      id: "friday",
      name: "Friday",
      role: "ceo",
      seeded: true,
    };
    // Cold first-run: roster present but the resolved CEO id not yet available,
    // so the assignee starts "" (Unassigned).
    const { rerender } = render(
      <NewIssueDialog {...baseProps} agents={[friday]} />,
    );
    expect(screen.getByLabelText(/assign to agent/i)).toHaveValue("");

    // User starts typing before the CEO id resolves.
    await user.type(screen.getByPlaceholderText("Task title"), "My first task");

    // The roster resolves → default assignee arrives. The dialog must seed it
    // via effect (not a remount) so the typed title survives.
    rerender(
      <NewIssueDialog
        {...baseProps}
        agents={[friday]}
        defaultAssigneeAgentId="friday"
      />,
    );
    expect(screen.getByLabelText(/assign to agent/i)).toHaveValue("friday");
    expect(screen.getByPlaceholderText("Task title")).toHaveValue(
      "My first task",
    );
  });

  test("seeds the Status select from defaultStatus", () => {
    render(<NewIssueDialog {...baseProps} defaultStatus="todo" />);
    expect(screen.getByLabelText("Status")).toHaveValue("todo");
  });

  test("Status defaults to backlog when defaultStatus is omitted", () => {
    render(<NewIssueDialog {...baseProps} />);
    expect(screen.getByLabelText("Status")).toHaveValue("backlog");
  });
});
