import { describe, test, expect, vi, beforeEach } from "vitest";
import { render, fireEvent, screen, waitFor } from "@testing-library/react";

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
    update: vi.fn(),
    addComment: vi.fn(),
  });
});

const baseProps = {
  open: true,
  onOpenChange: vi.fn(),
};

describe("NewIssueDialog", () => {
  test("renders dialog with title input and Create disabled when title empty", () => {
    render(<NewIssueDialog {...baseProps} />);
    expect(screen.getByPlaceholderText("Issue title")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /create issue/i })).toBeDisabled();
  });

  test("Create enabled once title is non-empty", () => {
    render(<NewIssueDialog {...baseProps} />);
    fireEvent.change(screen.getByPlaceholderText("Issue title"), {
      target: { value: "Fix" },
    });
    expect(screen.getByRole("button", { name: /create issue/i })).not.toBeDisabled();
  });

  test("submit fires create with trimmed inputs + priority default (status owned upstream)", async () => {
    mockCreate.mockResolvedValue({ id: "iss_new" });
    render(<NewIssueDialog {...baseProps} />);
    fireEvent.change(screen.getByPlaceholderText("Issue title"), {
      target: { value: "  Fix bug  " },
    });
    fireEvent.change(screen.getByPlaceholderText("Optional details..."), {
      target: { value: "Details" },
    });
    fireEvent.click(screen.getByRole("button", { name: /create issue/i }));
    await waitFor(() =>
      expect(mockCreate).toHaveBeenCalledWith({
        title: "Fix bug",
        description: "Details",
        priority: "medium",
        projectId: undefined,
        assigneeAgentId: undefined,
      }),
    );
  });

  test("onCreated fires with the created issue id", async () => {
    mockCreate.mockResolvedValue({ id: "iss_new" });
    const onCreated = vi.fn();
    render(<NewIssueDialog {...baseProps} onCreated={onCreated} />);
    fireEvent.change(screen.getByPlaceholderText("Issue title"), {
      target: { value: "Fix" },
    });
    fireEvent.click(screen.getByRole("button", { name: /create issue/i }));
    await waitFor(() => expect(onCreated).toHaveBeenCalledWith("iss_new"));
  });

  test("onOpenChange(false) called after successful create", async () => {
    mockCreate.mockResolvedValue({ id: "iss_new" });
    const onOpenChange = vi.fn();
    render(<NewIssueDialog open onOpenChange={onOpenChange} />);
    fireEvent.change(screen.getByPlaceholderText("Issue title"), {
      target: { value: "Fix" },
    });
    fireEvent.click(screen.getByRole("button", { name: /create issue/i }));
    await waitFor(() => expect(onOpenChange).toHaveBeenCalledWith(false));
  });

  test("error from create renders in alert", async () => {
    mockCreate.mockRejectedValue(new Error("server boom"));
    render(<NewIssueDialog {...baseProps} />);
    fireEvent.change(screen.getByPlaceholderText("Issue title"), {
      target: { value: "Fix" },
    });
    fireEvent.click(screen.getByRole("button", { name: /create issue/i }));
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
    mockCreate.mockResolvedValue({ id: "iss_new" });
    const projects: IssueProject[] = [{ id: "p1", name: "Inbox v1" }];
    const agents: CompanyAgent[] = [{ id: "ag_1", name: "Main Agent" }];
    render(<NewIssueDialog {...baseProps} projects={projects} agents={agents} />);
    fireEvent.change(screen.getByPlaceholderText("Issue title"), {
      target: { value: "Fix" },
    });
    fireEvent.change(screen.getByLabelText(/project/i), {
      target: { value: "p1" },
    });
    fireEvent.change(screen.getByLabelText(/assign to agent/i), {
      target: { value: "ag_1" },
    });
    fireEvent.click(screen.getByRole("button", { name: /create issue/i }));
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
});
