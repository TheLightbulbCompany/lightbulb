// @vitest-environment jsdom
import { render, screen } from "@testing-library/react";
import { vi, it, expect } from "vitest";

vi.mock("@/components/settings/SettingsPlace", () => ({
  PANELS: {
    members: () => <div>members ok</div>,
    approvals: () => { throw new Error("approvals exploded"); },
    connections: () => <div>connections ok</div>,
  },
}));
vi.mock("@/components/settings/DailySummaryCard", () => ({ DailySummaryCard: () => <div>daily ok</div> }));
vi.mock("@/components/settings/SessionNotificationsCard", () => ({ SessionNotificationsCard: () => <div>session ok</div> }));
vi.mock("../VoicePlanCard", () => ({ VoicePlanCard: () => null }));
vi.mock("../data", () => ({ useUsage: () => ({ data: undefined, isLoading: true }), useAgentConfig: () => ({ data: undefined, save: () => {} }) }));

import { VoiceSettings } from "../VoiceSettings";

it("one section throwing leaves the other sections on the page", () => {
  const quiet = vi.spyOn(console, "error").mockImplementation(() => {});
  render(<VoiceSettings />);
  quiet.mockRestore();
  expect(screen.getByText("members ok")).toBeInTheDocument();
  expect(screen.getByText("connections ok")).toBeInTheDocument();
  expect(screen.getByText("daily ok")).toBeInTheDocument();
  const broken = screen.getByRole("alert");
  expect(broken).toHaveTextContent("Approvals");
  expect(broken).toHaveTextContent("couldn't load");
});
