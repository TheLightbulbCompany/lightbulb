import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ProjectPicker } from "@/components/teams/issues/pickers/ProjectPicker";
import type { IssueProject } from "@/components/teams/shared/types";

const projects: IssueProject[] = [
  { id: "p_1", name: "Inbox v1" },
  { id: "p_2", name: "Inbox v2" },
];

describe("ProjectPicker", () => {
  it("renders the trigger with the current project's name", () => {
    render(
      <ProjectPicker
        projects={projects}
        selectedProjectId="p_1"
        onChange={vi.fn()}
      />,
    );
    expect(screen.getByRole("button", { name: /Inbox v1/ })).toBeInTheDocument();
  });

  it("renders 'No project' on the trigger when nothing is selected", () => {
    render(
      <ProjectPicker
        projects={projects}
        selectedProjectId={null}
        onChange={vi.fn()}
      />,
    );
    expect(screen.getByRole("button", { name: /No project/i })).toBeInTheDocument();
  });

  it("opens the popover and lists projects on trigger click", async () => {
    const user = userEvent.setup();
    render(
      <ProjectPicker
        projects={projects}
        selectedProjectId={null}
        onChange={vi.fn()}
      />,
    );
    await user.click(screen.getByRole("button", { name: /No project/i }));
    expect(await screen.findByText("Inbox v1")).toBeInTheDocument();
    expect(screen.getByText("Inbox v2")).toBeInTheDocument();
  });

  it("filters the list by the search query", async () => {
    const user = userEvent.setup();
    render(
      <ProjectPicker
        projects={projects}
        selectedProjectId={null}
        onChange={vi.fn()}
      />,
    );
    await user.click(screen.getByRole("button", { name: /No project/i }));
    const search = await screen.findByPlaceholderText(/Search projects/i);
    await user.type(search, "v2");
    expect(screen.getByText("Inbox v2")).toBeInTheDocument();
    expect(screen.queryByText("Inbox v1")).not.toBeInTheDocument();
  });

  it("calls onChange with the project id when an option is clicked", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <ProjectPicker
        projects={projects}
        selectedProjectId={null}
        onChange={onChange}
      />,
    );
    await user.click(screen.getByRole("button", { name: /No project/i }));
    await user.click(await screen.findByText("Inbox v2"));
    expect(onChange).toHaveBeenCalledWith("p_2");
  });

  it("calls onChange(null) when the None option is clicked", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <ProjectPicker
        projects={projects}
        selectedProjectId="p_1"
        onChange={onChange}
      />,
    );
    await user.click(screen.getByRole("button", { name: /Inbox v1/ }));
    await user.click(await screen.findByText(/Clear project/i));
    expect(onChange).toHaveBeenCalledWith(null);
  });
});
