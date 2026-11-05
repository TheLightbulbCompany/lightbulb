import { fireEvent, render, screen, waitFor } from "@testing-library/react-native";
import { ApiError, type QuestionView, type TaskDetail } from "@lightbulb/shared";
import { FIXTURES, PRIYA, SAM, viewAs } from "@lightbulb/shared/tasks/fixtures";
import type { ReactNode } from "react";

// Every out-of-scope name a jest.mock factory reaches for is `mock`-prefixed (babel-plugin-jest-hoist).
const mockAuth = { userId: SAM as string };
// One task id per test: the shared SWR cache keys `task:<id>` and `task-questions:<id>` are global (not per
// workspace), so a second test on the same id would read the first test's rows for SWR's dedupe window.
const mockParams = { taskId: "t-signup" };
const mockLive = { superAgent: true, tasks: null as unknown };
const mockRooms = {
  selectRoom: jest.fn(), messages: [] as unknown[], activeRoom: null, historyLoading: false, liveConnected: true,
  attachmentUrl: jest.fn(), uploadAttachment: jest.fn(), setTyping: jest.fn(),
};
const mockSend = jest.fn(async () => undefined);
jest.mock("expo-router", () => {
  const { useEffect } = require("react");
  return {
    Stack: { Screen: ({ options }: { options?: { headerTitle?: () => ReactNode } }) => (options?.headerTitle ? options.headerTitle() : null) },
    useFocusEffect: (cb: () => void) => useEffect(cb, [cb]),
    useLocalSearchParams: () => ({ taskId: mockParams.taskId }),
    router: { push: jest.fn() },
  };
});
jest.mock("@clerk/expo", () => ({ useAuth: () => ({ userId: mockAuth.userId }) }));
jest.mock("expo-crypto", () => ({ randomUUID: () => "00000000-0000-4000-8000-000000000000" }));
jest.mock("react-native-safe-area-context", () => ({ useSafeAreaInsets: () => ({ top: 0, bottom: 0, left: 0, right: 0 }) }));
jest.mock("../../../../providers/live-events", () => ({ useLiveEvents: () => mockLive, useSuperAgent: () => mockLive.superAgent }));
jest.mock("../../../../lib/rooms", () => ({
  useMobileRooms: () => mockRooms,
  useSendDrafts: () => ({ canSend: true, send: mockSend, retry: jest.fn(), rows: [] }),
  useTypingReport: () => () => {},
  cardReplySender: () => jest.fn(),
}));
jest.mock("../../../../components/room/RoomComposer", () => {
  const { Pressable, Text, View } = require("react-native");
  return {
    RoomComposer: ({ placeholder, onSend }: { placeholder: string; onSend: (d: unknown) => Promise<void> }) => (
      <View><Text>{placeholder}</Text><Pressable accessibilityRole="button" accessibilityLabel="Send steer" onPress={() => void onSend({ text: "use the brand purple" })}><Text>Send</Text></Pressable></View>
    ),
  };
});
jest.mock("../../../../components/ui/BottomSheet", () => {
  const { View } = require("react-native");
  return { BottomSheet: ({ visible, children }: { visible: boolean; children: ReactNode }) => (visible ? <View>{children}</View> : null) };
});

import TaskScreen from "../[taskId]";

const approval = (over: Partial<QuestionView> = {}): QuestionView => ({
  id: "q-provision", taskId: "t-signup", kind: "approval", subkind: null, title: "Provision database and Stripe",
  text: "Allow Lightbulb · Engineering to provision a database and Stripe payments?",
  options: [{ id: "deny", label: "Deny", style: "danger" }, { id: "approve", label: "Approve", style: "primary" }],
  state: "open", answerers: [{ id: SAM, name: "Sam" }], canAnswer: true, waitingLabel: "Waiting on you",
  answer: null, answeredBy: null, answeredAt: null, createdAt: "2026-11-04T14:00:00Z", version: 4, ...over,
});
const step8 = FIXTURES.step08.details["t-signup"];
const waiting = FIXTURES.step05.tasks[0];

function client(detail: TaskDetail, views: QuestionView[], id: string) {
  mockParams.taskId = id;
  detail = { ...detail, task: { ...detail.task, taskId: id }, taskRoomId: `task-room-${id}` };
  views = views.map((q) => ({ ...q, taskId: id }));
  const c = {
    cacheScope: `org_${Math.random()}`,
    list: jest.fn(async () => ({ tasks: [detail.task], next: null })),
    get: jest.fn(async () => detail),
    act: jest.fn(async () => detail.task),
    startMany: jest.fn(async () => ({ tasks: [] })),
    answer: jest.fn(async () => ({ question: views[0], next: null })),
    trace: jest.fn(async () => ({ attempt: 1, recentTools: [] })),
    markRead: jest.fn(async () => undefined),
    needsYou: jest.fn(async () => ({ items: [], next: null })),
    questions: jest.fn(async () => ({ items: views })),
  };
  mockLive.tasks = c;
  return c;
}
beforeEach(() => { mockAuth.userId = SAM; mockRooms.selectRoom.mockClear(); mockSend.mockClear(); });

it("non-answerer sees Waiting on Sam without buttons", async () => {
  mockAuth.userId = PRIYA;
  client({ ...step8, task: viewAs(waiting, PRIYA) }, [approval({ canAnswer: false, waitingLabel: "Waiting on Sam" })], "t-priya");
  const view = await render(<TaskScreen />);
  expect(await view.findByText("WAITING ON SAM")).toBeTruthy();
  expect(view.queryByLabelText("Approve")).toBeNull();
  expect(view.queryByLabelText("Deny")).toBeNull();
  await view.unmount();
});

it("approve posts optionId approve with the question's version; steer goes into the task room", async () => {
  const c = client({ ...step8, task: waiting }, [approval()], "t-approve");
  const view = await render(<TaskScreen />);
  fireEvent.press(await view.findByLabelText("Approve"));
  await waitFor(() => expect(c.answer).toHaveBeenCalledWith("t-approve", "q-provision", expect.objectContaining({ optionId: "approve", expectVersion: 4 })));
  expect(c.markRead).toHaveBeenCalledWith("t-approve");
  // The transcript is the task's hidden room; its composer steers it.
  expect(mockRooms.selectRoom).toHaveBeenCalledWith("task-room-t-approve");
  expect(view.getByText("Steer this thread…")).toBeTruthy();
  fireEvent.press(view.getByLabelText("Send steer"));
  await waitFor(() => expect(mockSend).toHaveBeenCalledWith({ text: "use the brand purple" }));
  await view.unmount();
});

it("409 shows who answered and removes the buttons, with no second POST", async () => {
  const byPriya = approval({ state: "answered", canAnswer: false, answeredBy: { id: PRIYA, name: "Priya" }, answeredAt: "2026-11-04T14:03:00Z", answer: { optionId: "approve", text: null } });
  const c = client({ ...step8, task: waiting }, [approval()], "t-409");
  c.questions.mockResolvedValueOnce({ items: [{ ...approval(), taskId: "t-409" }] }).mockResolvedValue({ items: [{ ...byPriya, taskId: "t-409" }] });
  c.answer.mockRejectedValueOnce(new ApiError(409, { code: "not_open" }));
  const view = await render(<TaskScreen />);
  fireEvent.press(await view.findByLabelText("Approve"));
  await waitFor(() => expect(view.queryByLabelText("Approve")).toBeNull());
  expect(c.answer).toHaveBeenCalledTimes(1);
  await view.unmount();
});
