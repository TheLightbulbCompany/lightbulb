import { test, expect, vi, beforeEach } from "vitest";
import { render, fireEvent, screen } from "@testing-library/react";

vi.mock("@/components/teams/command-palette/useFilteredCommandResults", () => ({
  useFilteredCommandResults: vi.fn(() => ({ agents: [], issues: [], projects: [] })),
}));
vi.mock("@/hooks/useLiveEvents", () => ({
  useLiveEvents: () => ({ messaging: { searchMessages: vi.fn().mockResolvedValue({ hits: [] }), warmSearch: vi.fn().mockResolvedValue(undefined) } }),
  useSuperAgent: () => false,
}));
vi.mock("@/components/settings/CompanyStripeSection", () => ({
  useCompanyStripeStatus: () => ({ data: null }),
}));

import { CommandPalette } from "@/components/teams/command-palette/CommandPalette";
import { useFilteredCommandResults } from "@/components/teams/command-palette/useFilteredCommandResults";

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(useFilteredCommandResults).mockReturnValue({ agents: [], issues: [], projects: [] });
});

test("renders 'Navigate' group with the IA nav actions on open with empty query", () => {
  render(<CommandPalette open onOpenChange={vi.fn()} />);
  expect(screen.getByText("Navigate")).toBeInTheDocument();
  expect(screen.getByText("Activity")).toBeInTheDocument();
  // The Inbox tab folded into Activity's For you: it is no longer offered.
  expect(screen.queryByText("Inbox")).not.toBeInTheDocument();
  // The org chart is a view of the Agents panel now, so "Agents" is the row.
  expect(screen.getByText("Agents")).toBeInTheDocument();
  expect(screen.getByText("Settings")).toBeInTheDocument();
});

test("typing in search filters nav actions", () => {
  render(<CommandPalette open onOpenChange={vi.fn()} />);
  const input = screen.getByPlaceholderText(/search messages/i);
  fireEvent.change(input, { target: { value: "mai" } });
  expect(screen.getByText("Mail")).toBeInTheDocument();
  expect(screen.queryByText("Activity")).not.toBeInTheDocument();
  // The old Inbox word still finds where its items went.
  fireEvent.change(input, { target: { value: "inbox" } });
  expect(screen.getByText("Activity")).toBeInTheDocument();
});

test("ArrowDown moves selection to next row", () => {
  render(<CommandPalette open onOpenChange={vi.fn()} />);
  const input = screen.getByPlaceholderText(/search/i);
  fireEvent.keyDown(input, { key: "ArrowDown" });
  // Search is item 1 (after Activity at 0)
  const searchRow = screen.getByText("Search").closest("[data-cmd-row]")!;
  expect(searchRow).toHaveAttribute("aria-selected", "true");
});

test("ArrowUp at top stays at 0", () => {
  render(<CommandPalette open onOpenChange={vi.fn()} />);
  const input = screen.getByPlaceholderText(/search/i);
  fireEvent.keyDown(input, { key: "ArrowUp" });
  const firstRow = screen.getByText("Activity").closest("[data-cmd-row]")!;
  expect(firstRow).toHaveAttribute("aria-selected", "true");
});

test("Enter on selected row pushes the canonical URL + closes dialog", () => {
  // Palette navigation commits through the History API (pushState of the
  // canonical in-shell URL), not the app router. Spy per-test — the global
  // afterEach restoreAllMocks() would detach a module-scope spy.
  const pushStateSpy = vi.spyOn(window.history, "pushState");
  const onOpenChange = vi.fn();
  render(<CommandPalette open onOpenChange={onOpenChange} />);
  const input = screen.getByPlaceholderText(/search/i);
  fireEvent.keyDown(input, { key: "Enter" });
  expect(pushStateSpy).toHaveBeenCalledWith(
    null,
    "",
    "/chat?panel=activity",
  );
  expect(onOpenChange).toHaveBeenCalledWith(false);
});

test("clicking a row selects it + navigates", () => {
  const pushStateSpy = vi.spyOn(window.history, "pushState");
  const onOpenChange = vi.fn();
  render(<CommandPalette open onOpenChange={onOpenChange} />);
  fireEvent.click(screen.getByText("Mail").closest("[data-cmd-row]")!);
  expect(pushStateSpy).toHaveBeenCalledWith(null, "", "/chat?panel=mail");
  expect(onOpenChange).toHaveBeenCalledWith(false);
});

test("shows 'No results.' when query yields nothing", () => {
  render(<CommandPalette open onOpenChange={vi.fn()} />);
  fireEvent.change(screen.getByPlaceholderText(/search/i), { target: { value: "zzzzzz" } });
  expect(screen.getByText("No results.")).toBeInTheDocument();
});

test("dynamic agents render when useFilteredCommandResults provides them", () => {
  vi.mocked(useFilteredCommandResults).mockReturnValue({
    agents: [{ id: "ag_1", name: "Main Agent" }],
    issues: [],
    projects: [],
  });
  render(<CommandPalette open onOpenChange={vi.fn()} />);
  // "Agents" appears both as a nav row label and as a group header — both
  // is what we want when an agent result is also present.
  expect(screen.getAllByText("Agents").length).toBeGreaterThanOrEqual(2);
  expect(screen.getByText("Main Agent")).toBeInTheDocument();
});

test("dynamic issues render with identifier as sublabel", () => {
  vi.mocked(useFilteredCommandResults).mockReturnValue({
    agents: [],
    issues: [{ id: "i1", title: "Fix bug", identifier: "PAP-42", status: "todo" }],
    projects: [],
  });
  render(<CommandPalette open onOpenChange={vi.fn()} />);
  // The nav row is "Tasks" (IA label); "Issues" is the dynamic-results group header.
  expect(screen.getByText("Tasks")).toBeInTheDocument();
  expect(screen.getAllByText("Issues").length).toBeGreaterThanOrEqual(1);
  expect(screen.getByText("Fix bug")).toBeInTheDocument();
  expect(screen.getByText("PAP-42")).toBeInTheDocument();
});

test("query state resets when dialog closes + reopens", () => {
  const { rerender } = render(<CommandPalette open onOpenChange={vi.fn()} />);
  const input = screen.getByPlaceholderText(/search/i) as HTMLInputElement;
  fireEvent.change(input, { target: { value: "inbo" } });
  expect(input.value).toBe("inbo");
  // Close + reopen
  rerender(<CommandPalette open={false} onOpenChange={vi.fn()} />);
  rerender(<CommandPalette open onOpenChange={vi.fn()} />);
  // After reopen, query should be ""
  const newInput = screen.getByPlaceholderText(/search/i) as HTMLInputElement;
  expect(newInput.value).toBe("");
});
