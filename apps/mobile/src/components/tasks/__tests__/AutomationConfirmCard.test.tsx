import { act, fireEvent, render, screen } from "@testing-library/react-native";

import { chatPalette } from "../../../config/theme";

const mockLive = { superAgent: true, tasks: { cacheScope: "org_confirm" }, agentNames: new Map([["agt-fin", "Finance"]]), api: { get: jest.fn(), post: jest.fn(async () => ({})), del: jest.fn(async () => undefined) } };
jest.mock("../../../providers/live-events", () => ({ useLiveEvents: () => mockLive, useSuperAgent: () => true }));
let mockRole = "org:admin";
jest.mock("@clerk/expo", () => ({ useAuth: () => ({ userId: "user_sam" }), useOrganization: () => ({ membership: { role: mockRole } }) }));
jest.mock("expo-router", () => ({ router: { push: jest.fn() } }));
jest.mock("expo-crypto", () => ({ randomUUID: () => "u" }));

import { AutomationConfirmCard } from "../AutomationConfirmCard";

const draft = {
  id: "auto-1", name: "Monday signup count", status: "draft", deliverTo: "room:r1", ownerUserId: "user_sam", specialistId: "agt-fin",
  instructions: "Post signups.", schedule: { cron: ["0 9 * * 1"], tz: "UTC" }, budgetPerRunUsd: 1, nextRuns: ["2026-11-09T09:00:00Z"],
  activeRunTaskId: null, match: null, version: 1,
};
beforeEach(() => {
  mockLive.tasks = { cacheScope: `org_${Math.random()}` };
  mockLive.api.get.mockReset().mockResolvedValue(draft);
  mockLive.api.post.mockClear();
  mockLive.api.del.mockClear();
});

it("an admin turns a room automation on; the card names the specialist and the next run", async () => {
  mockRole = "org:admin";
  const view = await render(<AutomationConfirmCard automationId="auto-1" palette={chatPalette.light} viewerTz="UTC" />);
  expect(await screen.findByText("Turn on: Monday signup count")).toBeTruthy();
  expect(screen.getByText(/Lightbulb · Finance/)).toBeTruthy();
  expect(screen.getByText(/Next: Mon 9 Nov, 9:00/)).toBeTruthy();
  await fireEvent.press(screen.getByLabelText("Turn on"));
  expect(mockLive.api.post).toHaveBeenCalledWith("/automations/auto-1/activate", {});
  await act(async () => {});
  await view.unmount();
});

it("a member waits on an admin; the owner can still say Not now, which deletes the draft", async () => {
  mockRole = "org:member";
  const view = await render(<AutomationConfirmCard automationId="auto-1" palette={chatPalette.light} viewerTz="UTC" />);
  expect(await screen.findByText("Waiting on a workspace admin")).toBeTruthy();
  expect(screen.queryByLabelText("Turn on")).toBeNull();
  await fireEvent.press(screen.getByLabelText("Not now"));
  expect(mockLive.api.del).toHaveBeenCalledWith("/automations/auto-1");
  expect(await screen.findByText("Not set up.")).toBeTruthy();
  await view.unmount();
});

it("a paused automation reads neutral and offers Turn on to whoever may activate it; no raw cron", async () => {
  mockRole = "org:admin";
  mockLive.api.get.mockReset().mockResolvedValue({ ...draft, status: "paused", pausedReason: null, schedule: { cron: ["0 9 1 * *"], tz: "UTC" } });
  const view = await render(<AutomationConfirmCard automationId="auto-1" palette={chatPalette.light} viewerTz="UTC" />);
  const paused = await screen.findByText("Paused");
  expect(paused.props.style).toEqual(expect.arrayContaining([expect.objectContaining({ color: chatPalette.light.fgMute })]));
  expect(screen.getByText(/Custom schedule/)).toBeTruthy();
  expect(screen.queryByText(/0 9 1/)).toBeNull();
  expect(screen.queryByLabelText("Not now")).toBeNull();
  await fireEvent.press(screen.getByLabelText("Turn on"));
  expect(mockLive.api.post).toHaveBeenCalledWith("/automations/auto-1/activate", {});
  await act(async () => {});
  await view.unmount();
});
