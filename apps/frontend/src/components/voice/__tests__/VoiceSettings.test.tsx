// @vitest-environment jsdom
import { render, screen } from "@testing-library/react";
import { vi, it, expect } from "vitest";

const conn = vi.hoisted(() => ({ connections: [] as { provider: string; status: string }[], startOAuth: vi.fn() }));
vi.mock("@/hooks/useConnections", () => ({ useConnections: () => ({ connections: conn.connections, isLoading: false, startOAuth: conn.startOAuth }) }));

vi.mock("@/components/settings/DailySummaryCard", () => ({ DailySummaryCard: () => <div>daily ok</div> }));
vi.mock("../VoicePlanCard", () => ({ VoicePlanCard: () => null }));
vi.mock("../PhonePinCard", () => ({ PhonePinCard: () => { throw new Error("pin exploded"); } }));
vi.mock("../data", () => ({ useUsage: () => ({ data: undefined, isLoading: true }), useAgentConfig: () => ({ data: undefined, save: () => {} }) }));

import { fireEvent, waitFor } from "@testing-library/react";
import { VoiceSettings } from "../VoiceSettings";

it("one section throwing leaves the other sections on the page", () => {
  const quiet = vi.spyOn(console, "error").mockImplementation(() => {});
  render(<VoiceSettings />);
  quiet.mockRestore();
  expect(screen.getByText("daily ok")).toBeInTheDocument();
  const broken = screen.getByRole("alert");
  expect(broken).toHaveTextContent("Phone PIN");
  expect(broken).toHaveTextContent("couldn't load");
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
  await waitFor(() => expect(screen.getByText(/Ask an admin of this business/)).toBeInTheDocument());
});
