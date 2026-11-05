import { act, fireEvent, render } from "@testing-library/react-native";
import { FIXTURES } from "@lightbulb/shared/tasks/fixtures";

const mockLive = { superAgent: true, tasks: null as unknown, api: { get: jest.fn(async () => { throw new Error("no route"); }), post: jest.fn() } };
const mockPush = jest.fn();
jest.mock("../../../providers/live-events", () => ({ useLiveEvents: () => mockLive, useSuperAgent: () => mockLive.superAgent }));
jest.mock("@clerk/expo", () => ({ useAuth: () => ({ userId: "user_sam" }), useOrganization: () => ({ membership: { role: "org:admin" } }) }));
jest.mock("expo-router", () => ({ router: { push: (...args: unknown[]) => mockPush(...args) } }));
jest.mock("expo-router/react-navigation", () => ({ HeaderHeightContext: require("react").createContext(0) }));
jest.mock("expo-crypto", () => ({ randomUUID: () => "u" }));

import { WorkScreen } from "../WorkScreen";

function client(tasks = FIXTURES.step03.tasks) {
  const c = { cacheScope: `org_${Math.random()}`, list: jest.fn(async () => ({ tasks, next: null })), questions: jest.fn(async () => ({ items: [] })) };
  mockLive.tasks = c;
  return c;
}
beforeEach(() => { mockPush.mockClear(); mockLive.api.get.mockClear(); });

// First, before any press: RNTL's async render after a test whose press is still settling can hand back an empty tree.
it("Task board (old) reaches today's board", async () => {
  client();
  const onOpenOldBoard = jest.fn();
  const view = await render(<WorkScreen onOpenOldBoard={onOpenOldBoard} />);
  await fireEvent.press(await view.findByLabelText("Task board (old)"));
  expect(onOpenOldBoard).toHaveBeenCalled();
  await act(async () => {});
  await view.unmount();
});

it("group order and placeholders: Waiting on you · Working · Resolved (collapsed)", async () => {
  client();
  const view = await render(<WorkScreen />);
  expect(await view.findByText("Working · 3")).toBeTruthy();
  const headings = () => view.getAllByText(/^(Waiting on you|Working|Ready for review|Landing|Idle|Resolved) · \d+/).map((n) => String(n.props.children).replace(/ ›$/, "").replace(/,/g, ""));
  expect(headings()).toEqual(["Waiting on you · 0", "Working · 3", "Resolved · 0"]);
  expect(view.getByText("Nothing needs you")).toBeTruthy();
  expect(view.queryByText("Nothing resolved yet")).toBeNull(); // collapsed by default
  await fireEvent.press(view.getByLabelText("Resolved · 0"));
  expect(await view.findByText("Nothing resolved yet")).toBeTruthy();
  expect(view.getByText("Workshop signup app")).toBeTruthy();
  await act(async () => {});
  await view.unmount();
});

it("rows push the task screen; Scheduled shows the empty state until automations exist", async () => {
  client(FIXTURES.step05.tasks);
  const view = await render(<WorkScreen />);
  await fireEvent.press(await view.findByLabelText("Open Workshop signup app, Waiting on you"));
  expect(mockPush).toHaveBeenCalledWith({ pathname: "/(app)/task/[taskId]", params: { taskId: "t-signup" } });
  await fireEvent.press(view.getByText("Scheduled"));
  expect(await view.findByText("Ask @lightbulb to put recurring work on a schedule.")).toBeTruthy();
  expect(mockLive.api.get).toHaveBeenCalledWith("/automations");
  await act(async () => {});
  await view.unmount();
});
