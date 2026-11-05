import { act, fireEvent, render, screen, waitFor } from "@testing-library/react-native";
import { FIXTURES, PRIYA, SAM, viewAs } from "@lightbulb/shared/tasks/fixtures";

import { chatPalette } from "../../../config/theme";

const live = { superAgent: true, tasks: null as unknown };
jest.mock("../../../providers/live-events", () => ({ useLiveEvents: () => live, useSuperAgent: () => live.superAgent }));
jest.mock("expo-router", () => ({ router: { push: jest.fn() } }));
jest.mock("expo-crypto", () => ({ randomUUID: () => "00000000-0000-4000-8000-000000000000" }));

import { MessageTasks, TaskCard } from "../TaskCard";

const p = chatPalette.light;
const byId = (step: keyof typeof FIXTURES, id: string) => FIXTURES[step].tasks.find((t) => t.taskId === id)!;
const Fresh = ({ children }: { children: React.ReactNode }) => <>{children}</>;

function fakeClient() {
  return {
    cacheScope: "org_card_test",
    list: jest.fn(async () => ({ tasks: FIXTURES.step05.tasks, next: null })),
    get: jest.fn(async () => undefined),
    act: jest.fn(async () => byId("step03", "t-fees")),
    startMany: jest.fn(async () => ({ tasks: [] })),
    answer: jest.fn(async () => ({ question: {}, next: null })),
    trace: jest.fn(async () => ({ attempt: 1, recentTools: [] })),
    markRead: jest.fn(async () => undefined),
    needsYou: jest.fn(async () => ({ items: [], next: null })),
    questions: jest.fn(async () => ({ items: [{ id: "q-provision", version: 7 }] })),
  };
}
let client: ReturnType<typeof fakeClient>;
beforeEach(() => { client = fakeClient(); live.tasks = client; });

it("a missing task reads as unavailable", async () => {
  // First in the file: RNTL's async render after a test whose action is still settling can hand back
  // an empty tree (seen with the two tests below ahead of it), so this one runs before any press.
  const view = await render(<Fresh><TaskCard task={undefined} taskId="gone" viewerId={SAM} palette={p} /></Fresh>);
  expect(view.getByText("This thread isn't available")).toBeTruthy();
});

it("word and action per viewer: Sam sees Approve, Priya sees Waiting on Sam", async () => {
  const waiting = byId("step05", "t-signup");
  const sam = await render(<Fresh><TaskCard task={waiting} taskId="t-signup" viewerId={SAM} palette={p} /></Fresh>);
  expect(screen.getByText("Waiting on you")).toBeTruthy();
  expect(screen.getByText("Lightbulb · Engineering")).toBeTruthy();
  expect(screen.getByText("Needs approval: Provision database and Stripe")).toBeTruthy();
  expect(screen.getByLabelText("Approve")).toBeTruthy();
  await sam.unmount();

  const priya = await render(<Fresh><TaskCard task={viewAs(waiting, PRIYA)} taskId="t-signup" viewerId={PRIYA} palette={p} /></Fresh>);
  expect(screen.getByText("Waiting on Sam")).toBeTruthy();
  expect(screen.queryByLabelText("Approve")).toBeNull();
  await priya.unmount();
});

it("the card is one button with an accessible name; Approve opens the thread screen, never answers blind", async () => {
  const onOpen = jest.fn();
  const view = await render(<Fresh><TaskCard task={byId("step05", "t-signup")} taskId="t-signup" viewerId={SAM} palette={p} onOpen={onOpen} /></Fresh>);
  const card = screen.getByLabelText("Open Workshop signup app, Waiting on you");
  expect(card.props.accessibilityRole).toBe("button");
  fireEvent.press(card);
  expect(onOpen).toHaveBeenCalledWith("t-signup");
  onOpen.mockClear();
  fireEvent.press(screen.getByLabelText("Approve"));
  expect(onOpen).toHaveBeenCalledWith("t-signup");
  expect(client.answer).not.toHaveBeenCalled();
  await act(async () => {});
  await view.unmount();
});

it("a card for a thread older than the room's first page reads it by id", async () => {
  const old = { ...byId("step05", "t-signup"), taskId: "t-old", title: "Last month's report" };
  client.get.mockResolvedValue({ task: old, attempts: [], questions: [], effects: [], subscriptions: [], taskRoomId: "tr" } as never);
  const message = { roomId: "r-old", taskRefs: [{ taskId: "t-old", role: "started" }] } as never;
  const view = await render(<Fresh><MessageTasks message={message} viewerId={SAM} palette={p} /></Fresh>);
  expect(await screen.findByText("Last month's report")).toBeTruthy();
  expect(client.get).toHaveBeenCalledWith("t-old");
  expect(screen.queryByText("This thread isn't available")).toBeNull();
  await act(async () => {});
  await view.unmount();
});
