// Tests for ProjectProperties — the inline Status picker (PATCH `{ status }`)
// and the Goals multi-select (PATCH `{ goal_ids }` with the FULL desired set)
// in the project-detail aside. Verifies the current status renders as an
// editable control, the "backlog" default fallback, that current goal links
// render as chips, and that add/remove send the whole id array (replace
// semantics — never a blind diff that could drop an unrelated link).

import { describe, test, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { ProjectProperties } from "@/components/teams/projects/ProjectProperties";
import type {
  Project,
  ProjectGoalRef,
} from "@/components/teams/shared/types";

function makeProject(overrides: Partial<Project> = {}): Project {
  return {
    id: "p_1",
    name: "Marketing",
    ...overrides,
  };
}

const COMPANY_GOALS: ProjectGoalRef[] = [
  { id: "g_1", title: "Grow revenue" },
  { id: "g_2", title: "Improve retention" },
  { id: "g_3", title: "Expand to EU" },
];

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

describe("ProjectProperties goals multi-select", () => {
  test("renders currently-linked goals (from the payload `goals` refs) as chip links", () => {
    render(
      <ProjectProperties
        project={makeProject({
          goalIds: ["g_1", "g_2"],
          goals: [
            { id: "g_1", title: "Grow revenue" },
            { id: "g_2", title: "Improve retention" },
          ],
        })}
        issueCount={0}
        goals={COMPANY_GOALS}
        onStatusChange={vi.fn()}
        onGoalsChange={vi.fn()}
      />,
    );
    const revenue = screen.getByRole("link", { name: "Grow revenue" });
    expect(revenue).toBeInTheDocument();
    expect(revenue).toHaveAttribute(
      "href",
      "/chat?panel=teams-goal-detail&detailId=g_1",
    );
    expect(
      screen.getByRole("link", { name: "Improve retention" }),
    ).toBeInTheDocument();
  });

  test("resolves chip titles from the company goals list when the payload carries only goalIds", () => {
    render(
      <ProjectProperties
        project={makeProject({ goalIds: ["g_3"] })}
        issueCount={0}
        goals={COMPANY_GOALS}
        onStatusChange={vi.fn()}
        onGoalsChange={vi.fn()}
      />,
    );
    expect(
      screen.getByRole("link", { name: "Expand to EU" }),
    ).toBeInTheDocument();
  });

  test("removing a goal PATCHes the FULL remaining id set", async () => {
    const user = userEvent.setup();
    const onGoalsChange = vi.fn();
    render(
      <ProjectProperties
        project={makeProject({
          goalIds: ["g_1", "g_2"],
          goals: [
            { id: "g_1", title: "Grow revenue" },
            { id: "g_2", title: "Improve retention" },
          ],
        })}
        issueCount={0}
        goals={COMPANY_GOALS}
        onStatusChange={vi.fn()}
        onGoalsChange={onGoalsChange}
      />,
    );
    await user.click(
      screen.getByRole("button", { name: "Remove goal Grow revenue" }),
    );
    // Full desired set sent (the survivor), not just the removed id.
    expect(onGoalsChange).toHaveBeenCalledWith(["g_2"]);
  });

  test("adding a goal PATCHes the FULL set including the new id", async () => {
    const user = userEvent.setup();
    const onGoalsChange = vi.fn();
    render(
      <ProjectProperties
        project={makeProject({
          goalIds: ["g_1"],
          goals: [{ id: "g_1", title: "Grow revenue" }],
        })}
        issueCount={0}
        goals={COMPANY_GOALS}
        onStatusChange={vi.fn()}
        onGoalsChange={onGoalsChange}
      />,
    );
    // Open the add popover and pick an unlinked goal.
    await user.click(screen.getByRole("button", { name: /Goal/ }));
    await user.click(screen.getByRole("button", { name: "Improve retention" }));
    expect(onGoalsChange).toHaveBeenCalledWith(["g_1", "g_2"]);
  });

  test("only unlinked goals appear in the add picker", async () => {
    const user = userEvent.setup();
    render(
      <ProjectProperties
        project={makeProject({
          goalIds: ["g_1"],
          goals: [{ id: "g_1", title: "Grow revenue" }],
        })}
        issueCount={0}
        goals={COMPANY_GOALS}
        onStatusChange={vi.fn()}
        onGoalsChange={vi.fn()}
      />,
    );
    await user.click(screen.getByRole("button", { name: /Goal/ }));
    // g_1 is already linked → not offered; g_2/g_3 are.
    expect(
      screen.getByRole("button", { name: "Improve retention" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Expand to EU" }),
    ).toBeInTheDocument();
    // The only "Grow revenue" present is the chip link, not an add-option button.
    expect(
      screen.queryByRole("button", { name: "Grow revenue" }),
    ).not.toBeInTheDocument();
  });

  test("renders a dash and no add control when there are no links and editing is disabled", () => {
    render(
      <ProjectProperties
        project={makeProject()}
        issueCount={0}
        onStatusChange={vi.fn()}
      />,
    );
    // No onGoalsChange → read-only: the Goals row shows the empty dash.
    const dashes = screen.getAllByText("—");
    expect(dashes.length).toBeGreaterThan(0);
    expect(
      screen.queryByRole("button", { name: /Goal/ }),
    ).not.toBeInTheDocument();
  });
});
