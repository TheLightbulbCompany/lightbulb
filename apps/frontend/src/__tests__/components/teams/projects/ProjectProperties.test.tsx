// Tests for ProjectProperties — the inline Status picker (PATCH `{ status }`)
// added to the project-detail aside. Verifies the current status renders as an
// editable control and that an absent/unknown status falls back to the
// upstream "backlog" default rather than hiding the row.

import { describe, test, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";

import { ProjectProperties } from "@/components/teams/projects/ProjectProperties";
import type { Project } from "@/components/teams/shared/types";

function makeProject(overrides: Partial<Project> = {}): Project {
  return {
    id: "p_1",
    name: "Marketing",
    ...overrides,
  };
}

describe("ProjectProperties status picker", () => {
  test("renders the current status as an editable button", () => {
    render(
      <ProjectProperties
        project={makeProject({ status: "in_progress" })}
        issueCount={3}
        onStatusChange={vi.fn()}
      />,
    );
    expect(
      screen.getByRole("button", { name: "In Progress" }),
    ).toBeInTheDocument();
  });

  test("falls back to Backlog when status is absent", () => {
    render(
      <ProjectProperties
        project={makeProject()}
        issueCount={0}
        onStatusChange={vi.fn()}
      />,
    );
    expect(screen.getByRole("button", { name: "Backlog" })).toBeInTheDocument();
  });

  test("falls back to Backlog for an unknown status value", () => {
    render(
      <ProjectProperties
        project={makeProject({ status: "weird" })}
        issueCount={0}
        onStatusChange={vi.fn()}
      />,
    );
    expect(screen.getByRole("button", { name: "Backlog" })).toBeInTheDocument();
  });
});
