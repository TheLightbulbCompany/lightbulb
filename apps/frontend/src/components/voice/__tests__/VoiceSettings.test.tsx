// @vitest-environment jsdom
import { render, screen } from "@testing-library/react";
import { vi, it, expect, beforeEach } from "vitest";

const desktop = vi.hoisted(() => ({ enabled: false }));
vi.mock("@/lib/desktopNotify", () => ({ isDesktopApp: () => desktop.enabled }));
vi.mock("@/components/desktop/DesktopUpdater", () => ({ DesktopUpdater: () => <button type="button">Check for updates</button> }));
vi.mock("@/hooks/useConnections", () => ({ useConnections: () => ({ connections: [], startOAuth: vi.fn() }) }));
beforeEach(() => { desktop.enabled = false; });

vi.mock("@/components/settings/WorkspaceMembersSection", () => ({ WorkspaceMembersSection: () => { throw new Error("members exploded"); } }));
vi.mock("@/components/settings/DailySummaryCard", () => ({ DailySummaryCard: () => <div>daily ok</div> }));
vi.mock("../VoicePlanCard", () => ({ VoicePlanCard: () => null }));
vi.mock("../PhonePinCard", () => ({ PhonePinCard: () => null }));
vi.mock("../data", () => ({ useUsage: () => ({ data: undefined, isLoading: true }), useAgentConfig: () => ({ data: undefined, save: () => {} }) }));

import { VoiceSettings } from "../VoiceSettings";

it("one section throwing leaves the other sections on the page", () => {
  const quiet = vi.spyOn(console, "error").mockImplementation(() => {});
  render(<VoiceSettings />);
  quiet.mockRestore();
  expect(screen.getByText("daily ok")).toBeInTheDocument();
  const broken = screen.getByRole("alert");
  expect(broken).toHaveTextContent("Members");
  expect(broken).toHaveTextContent("couldn't load");
});

it.each([true, false])("shows the existing desktop updater only in the desktop app (desktop=%s)", (enabled) => {
  desktop.enabled = enabled;
  const quiet = vi.spyOn(console, "error").mockImplementation(() => {});
  render(<VoiceSettings />);
  quiet.mockRestore();
  if (enabled) {
    expect(screen.getByRole("heading", { name: "Desktop updates" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Check for updates" })).toBeInTheDocument();
  } else {
    expect(screen.queryByRole("heading", { name: "Desktop updates" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Check for updates" })).not.toBeInTheDocument();
  }
});
