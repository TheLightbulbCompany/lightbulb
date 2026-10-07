import { test, expect, vi } from "vitest";
// Smoke tests for IssueFiltersPopover — trigger render + popover open.
// Upstream prop surface: { state, onChange, activeFilterCount, agents?, projects?,
// labels?, currentUserId?, enableRoutineVisibilityFilter?, buttonVariant?, iconOnly?,
// workspaces?, creators? }. `defaultIssueFilterState` is a const object (not a fn).

import { render, fireEvent } from "@testing-library/react";
import { IssueFiltersPopover } from "@/components/teams/inbox/IssueFiltersPopover";
import { defaultIssueFilterState } from "@/components/teams/shared/lib/issueFilters";

const baseProps = {
  state: defaultIssueFilterState,
  onChange: vi.fn(),
  activeFilterCount: 0,
  agents: [],
  projects: [],
  labels: [],
  workspaces: [],
  creators: [],
  currentUserId: "u1",
};

test("renders trigger button with default 'Filter' label", () => {
  const { getByRole } = render(<IssueFiltersPopover {...baseProps} />);
  expect(getByRole("button", { name: /filter/i })).toBeInTheDocument();
});

test("trigger reflects active filter count", () => {
  const { getByRole } = render(
    <IssueFiltersPopover {...baseProps} activeFilterCount={3} />,
  );
  // The visible label becomes "Filters: 3" when there are active filters.
  expect(getByRole("button", { name: /filters: 3/i })).toBeInTheDocument();
});

test("opens popover on trigger click and renders status / priority filters", () => {
  const { getByRole, getAllByRole } = render(<IssueFiltersPopover {...baseProps} />);
  fireEvent.click(getByRole("button", { name: /filter/i }));
  // Popover content rendered — status (7) + priority (4) + visibility (1) checkboxes.
  expect(getAllByRole("checkbox").length).toBeGreaterThan(0);
});
