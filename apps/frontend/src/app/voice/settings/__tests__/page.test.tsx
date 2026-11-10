import { render, screen, fireEvent } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";

const hooks = vi.hoisted(() => ({ useUsage: vi.fn(), useAgentConfig: vi.fn(), usePlan: vi.fn() }));
vi.mock("@/components/voice/data", () => hooks);
// Keep the real VoicePlanCard; isolate its server state and payment boundary.
vi.mock("swr", () => ({ default: hooks.usePlan }));
vi.mock("@/lib/api", () => ({ useApi: () => ({}) }));
vi.mock("@/hooks/useIsWorkspaceAdmin", () => ({ useIsWorkspaceAdmin: () => true }));
vi.mock("@/hooks/useConnections", () => ({ useConnections: () => ({ connections: [{ provider: "google", status: "disconnected" }], startOAuth: vi.fn() }) }));
vi.mock("@/components/settings/WorkspaceMembersSection", () => ({ WorkspaceMembersSection: () => <p>members</p> }));
vi.mock("@/components/settings/DailySummaryCard", () => ({ DailySummaryCard: () => <p>daily</p> }));

import Page from "../page";

const save = vi.fn();
beforeEach(() => {
  save.mockReset();
  hooks.useUsage.mockReturnValue({ data: { used: 38, included: 1500, overageRate: 0.35, runOut: "keep", paidAllowance: true } });
  hooks.useAgentConfig.mockReturnValue({ data: { name: "Nova" }, save });
  hooks.usePlan.mockReturnValue({ data: {
    ownerId: "user_test_123", status: "active", available: true, planVersion: "voice-agents-v1",
    monthlyUsd: 250, includedMinutes: 1500, overageUsdPerMinute: 0.35,
  } });
});

it("Billing card shows 38 of 1,500 min", () => {
  render(<Page />);
  expect(screen.getByText(/of 1,500 min used this month/)).toBeInTheDocument();
  expect(screen.getByText("38")).toBeInTheDocument();
  expect(screen.getByText(/\$250\/month includes 1,500 voice minutes\. Additional minutes cost \$0.35 each\./)).toBeInTheDocument();
  expect(screen.getByRole("radio", { name: /Keep answering at \$0.35\/min/ })).toBeChecked();
  expect(screen.getByRole("meter")).toHaveAttribute("aria-valuenow", "38");
  expect(screen.getByRole("meter")).toHaveAttribute("aria-valuemax", "1500");
});

it("shows only the salon-owner sections, none of the old chat-product ones", () => {
  render(<Page />);
  for (const t of ["members", "daily"]) expect(screen.getByText(t)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Connect Google Calendar" })).toBeInTheDocument();
  for (const t of [/workspace billing/, /Approvals/, /Inference provider/, /Session notifications/]) expect(screen.queryByText(t)).not.toBeInTheDocument();
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

it("saves the run-out choice into the Agent document", () => {
  render(<Page />);
  expect(screen.getByLabelText(/Keep answering/)).toBeChecked();
  fireEvent.click(screen.getByLabelText(/Send calls to my phone/));
  expect(save).toHaveBeenCalledTimes(1);
  expect(save).toHaveBeenCalledWith({ name: "Nova", runOut: "forward" });
});

it("uses the persisted Agent run-out choice before the usage fallback and can save keep", () => {
  hooks.useAgentConfig.mockReturnValue({ data: { name: "Nova", runOut: "forward" }, save });
  render(<Page />);
  expect(screen.getByRole("radio", { name: /Send calls to my phone/ })).toBeChecked();
  expect(screen.getByRole("radio", { name: /Keep answering/ })).not.toBeChecked();
  fireEvent.click(screen.getByRole("radio", { name: /Keep answering/ }));
  expect(save).toHaveBeenCalledTimes(1);
  expect(save).toHaveBeenCalledWith({ name: "Nova", runOut: "keep" });
});

it("shows measured usage without allowance or run-out controls when paid access is unconfirmed", () => {
  hooks.useUsage.mockReturnValue({ data: { used: 38, included: 1500, overageRate: 0.35, runOut: "keep", paidAllowance: false } });
  render(<Page />);
  expect(screen.getByText("38")).toBeInTheDocument();
  expect(screen.getByText(/voice min used this month/)).toBeInTheDocument();
  expect(screen.getByText(/Included minutes apply while your voice plan has confirmed paid access/)).toBeInTheDocument();
  expect(screen.queryByText(/of 1,500 min used/)).not.toBeInTheDocument();
  expect(screen.queryByRole("meter")).not.toBeInTheDocument();
  expect(screen.queryByRole("radio")).not.toBeInTheDocument();
  expect(save).not.toHaveBeenCalled();
});
