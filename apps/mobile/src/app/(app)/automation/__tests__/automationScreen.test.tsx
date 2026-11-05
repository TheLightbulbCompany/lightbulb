import { render } from "@testing-library/react-native";
import type { AutomationView } from "@lightbulb/shared";

const mockLive = { superAgent: true, tasks: { cacheScope: "org_a" }, api: { get: jest.fn(), post: jest.fn() } };
jest.mock("../../../../providers/live-events", () => ({ useLiveEvents: () => mockLive, useSuperAgent: () => mockLive.superAgent }));
jest.mock("@clerk/expo", () => ({ useAuth: () => ({ userId: "user_sam" }), useOrganization: () => ({ membership: { role: "org:member" } }) }));
jest.mock("expo-router", () => ({ Stack: { Screen: () => null }, useLocalSearchParams: () => ({ id: "auto-1" }), router: { push: jest.fn() } }));
jest.mock("expo-crypto", () => ({ randomUUID: () => "u" }));

import AutomationScreen from "../[id]";

const row = (over: Partial<AutomationView> = {}) => ({
  id: "auto-1", name: "Monday signup count", status: "active", deliverTo: "room:r1", ownerUserId: "user_sam", specialistId: "finance",
  instructions: "Post signups so far.", schedule: { cron: ["0 9 * * 1"], tz: "UTC" }, budgetPerRunUsd: 1, activeRunTaskId: null, match: null, ...over,
}) as unknown as AutomationView;

function routes(a: AutomationView) {
  mockLive.tasks = { cacheScope: `org_${Math.random()}` };
  mockLive.api.get.mockReset().mockImplementation(async (path: string) => (path.endsWith("/runs") ? { runs: [], next: null } : a));
}
beforeEach(() => { mockLive.superAgent = true; });

it("switch off: a pasted automation link renders nothing and fetches nothing", async () => {
  mockLive.superAgent = false;
  routes(row());
  const view = await render(<AutomationScreen />);
  expect(view.toJSON()).toBeNull();
  expect(mockLive.api.get).not.toHaveBeenCalled();
  await view.unmount();
});

it("paused: Run now is off with its reason; a member waits on an admin to resume a room automation", async () => {
  routes(row({ status: "paused" }));
  const view = await render(<AutomationScreen />);
  expect(await view.findByText("Resume it first")).toBeTruthy();
  expect(view.getByLabelText("Run now")).toBeDisabled();
  expect(view.getByText("Waiting on a workspace admin")).toBeTruthy();
  expect(view.queryByLabelText("Resume")).toBeNull();
  await view.unmount();
});
