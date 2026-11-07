import { render, screen, fireEvent } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";

const hooks = vi.hoisted(() => ({ useUsage: vi.fn(), useAgentConfig: vi.fn() }));
vi.mock("@/components/voice/data", () => hooks);
vi.mock("@/components/settings/SettingsPlace", () => ({
  PANELS: { members: () => <p>members</p>, connections: () => <p>connections</p>, approvals: () => <p>approvals</p> },
}));
vi.mock("@/components/settings/DailySummaryCard", () => ({ DailySummaryCard: () => <p>daily</p> }));
vi.mock("@/components/settings/SessionNotificationsCard", () => ({ SessionNotificationsCard: () => <p>session</p> }));

import Page from "../page";

const save = vi.fn();
beforeEach(() => {
  save.mockReset();
  hooks.useUsage.mockReturnValue({ data: { used: 38, included: 1500, overageRate: 0.35, runOut: "keep" } });
  hooks.useAgentConfig.mockReturnValue({ data: { name: "Nova" }, save });
});

it("Billing card shows 38 of 1,500 min", () => {
  render(<Page />);
  expect(screen.getByText(/of 1,500 min used this month/)).toBeInTheDocument();
  expect(screen.getByText("38")).toBeInTheDocument();
  expect(screen.getByText(/After that, \$0.35 a minute/)).toBeInTheDocument();
  expect(screen.getByRole("meter")).toHaveAttribute("aria-valuenow", "38");
});

it("shows the existing Members, Connections, Approvals and Notifications sections", () => {
  render(<Page />);
  for (const t of ["members", "connections", "approvals", "daily", "session"]) expect(screen.getByText(t)).toBeInTheDocument();
});

it("saves the run-out choice into the Agent document", () => {
  render(<Page />);
  expect(screen.getByLabelText(/Keep answering/)).toBeChecked();
  fireEvent.click(screen.getByLabelText(/Send calls to my phone/));
  expect(save).toHaveBeenCalledWith({ name: "Nova", runOut: "forward" });
});
