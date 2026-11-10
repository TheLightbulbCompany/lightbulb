// @vitest-environment jsdom
import { render, screen } from "@testing-library/react";
import { vi, it, expect, beforeEach } from "vitest";

const desktop = vi.hoisted(() => ({ enabled: false }));
vi.mock("@/lib/desktopNotify", () => ({ isDesktopApp: () => desktop.enabled }));
vi.mock("@/components/desktop/DesktopUpdater", () => ({ DesktopUpdater: () => <button type="button">Check for updates</button> }));
const conn = vi.hoisted(() => ({ connections: [] as { provider: string; status: string }[], startOAuth: vi.fn() }));
vi.mock("@/hooks/useConnections", () => ({ useConnections: () => ({ connections: conn.connections, isLoading: false, startOAuth: conn.startOAuth }) }));
beforeEach(() => { desktop.enabled = false; });

vi.mock("@/components/settings/WorkspaceMembersSection", () => ({ WorkspaceMembersSection: () => { throw new Error("members exploded"); } }));
vi.mock("@/components/settings/DailySummaryCard", () => ({ DailySummaryCard: () => <div>daily ok</div> }));
vi.mock("../VoicePlanCard", () => ({ VoicePlanCard: () => null }));
vi.mock("../PhonePinCard", () => ({ PhonePinCard: () => null }));
vi.mock("../data", () => ({ useUsage: () => ({ data: undefined, isLoading: true }), useAgentConfig: () => ({ data: undefined, save: () => {} }) }));

import { fireEvent, waitFor } from "@testing-library/react";
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

it("calendar card: connected shows the tag and no button", () => {
  conn.connections = [{ provider: "google", status: "connected" }];
  render(<VoiceSettings />);
  expect(screen.getByText("Connected")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /Google/ })).not.toBeInTheDocument();
});

it("calendar card: reconnect_required offers Reconnect, and a 403 shows an admin hint", async () => {
  conn.connections = [{ provider: "google", status: "reconnect_required" }];
  conn.startOAuth.mockRejectedValue({ status: 403 });
  render(<VoiceSettings />);
  fireEvent.click(screen.getByRole("button", { name: "Reconnect Google" }));
  await waitFor(() => expect(screen.getByText(/Ask a workspace admin/)).toBeInTheDocument());
});
