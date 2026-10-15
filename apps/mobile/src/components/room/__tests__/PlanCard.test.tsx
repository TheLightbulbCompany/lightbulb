import { fireEvent, render, screen, waitFor } from "@testing-library/react-native";

// A fresh org per test: the /ideas SWR cache is global here, keyed by user and org.
let mockOrg = 0;
jest.mock("@clerk/expo", () => ({ useAuth: () => ({ userId: "me", orgId: `org-${mockOrg}`, getToken: async () => "t" }) }), { virtual: true });
jest.mock("../../chat/MessageMarkdown", () => {
  const { Text } = jest.requireActual("react-native");
  return { MessageMarkdown: ({ text }: { text: string }) => <Text>{text}</Text> };
});

const mockApproval = { current: { id: "ap-1", status: "pending", type: "request_board_approval",
  payload: { title: "Plan: Your CRM set up", summary: "Import, then three workflows." } } };
const mockGet = jest.fn(async (_id: string) => mockApproval.current);
const mockApprove = jest.fn(async (_id: string) => {});
const mockRevise = jest.fn(async (_id: string, _note: string) => {});
jest.mock("../../../lib/teamsData", () => ({
  useTeamsConfig: () => ({}),
  getApproval: (_c: unknown, id: string) => mockGet(id),
  approveApproval: (_c: unknown, id: string) => mockApprove(id),
  requestApprovalRevision: (_c: unknown, id: string, note: string) => mockRevise(id, note),
}));
const mockListIdeas = jest.fn();
jest.mock("../../../providers/live-events", () => ({
  useLiveEvents: () => ({ messaging: { listIdeas: mockListIdeas, startIdea: jest.fn(), dismissIdea: jest.fn() } }),
}));

import { DmIdeaPlan, PlanCard } from "../PlanCard";

beforeEach(() => {
  mockOrg += 1;
  jest.clearAllMocks();
  mockApproval.current = { ...mockApproval.current, status: "pending" };
});

it("a pending plan offers Approve plan, which approves it", async () => {
  await render(<PlanCard approvalId="ap-1" />);
  expect(await screen.findByText("Plan: Your CRM set up")).toBeTruthy();
  expect(screen.getByText("Import, then three workflows.")).toBeTruthy();
  await fireEvent.press(screen.getByRole("button", { name: "Approve plan" }));
  await waitFor(() => expect(mockApprove).toHaveBeenCalledWith("ap-1"));
});

it("a decided plan shows its status and no buttons", async () => {
  mockApproval.current = { ...mockApproval.current, status: "revision_requested" };
  await render(<PlanCard approvalId="ap-1" />);
  expect(await screen.findByText("You asked for changes. The plan is being revised.")).toBeTruthy();
  expect(screen.queryByRole("button")).toBeNull();
});

it("Change something needs a note, sends it as the decision note, and Cancel goes back", async () => {
  await render(<PlanCard approvalId="ap-1" />);
  await fireEvent.press(await screen.findByRole("button", { name: "Change something" }));
  const send = () => screen.getByRole("button", { name: "Send" });
  expect(send().props.accessibilityState.disabled).toBe(true);
  await fireEvent.changeText(screen.getByLabelText("What should change?"), "   ");
  expect(send().props.accessibilityState.disabled).toBe(true);
  await fireEvent.press(screen.getByRole("button", { name: "Cancel" }));
  expect(screen.getByRole("button", { name: "Approve plan" })).toBeTruthy();

  await fireEvent.press(screen.getByRole("button", { name: "Change something" }));
  await fireEvent.changeText(screen.getByLabelText("What should change?"), " Start with the import ");
  await fireEvent.press(send());
  await waitFor(() => expect(mockRevise).toHaveBeenCalledWith("ap-1", "Start with the import"));
});

it("the DM shows the plan GET /ideas matched to the viewer's start in this room", async () => {
  mockListIdeas.mockResolvedValue({ ideas: [], running: [{ ideaId: "crm", title: "CRM", agentId: "g", agentName: "Growth",
    roomId: "dm-1", projectId: "p", starterId: "me", starterName: "Me", createdAt: new Date().toISOString(),
    plan: { approvalId: "ap-1", status: "pending", title: "Plan: Your CRM set up" } }] });
  await render(<DmIdeaPlan roomId="dm-1" />);
  expect(await screen.findByRole("button", { name: "Approve plan" })).toBeTruthy();
  expect(mockGet).toHaveBeenCalledWith("ap-1");
});

it("a plan that could not be read says so and retries on tap", async () => {
  mockGet.mockRejectedValue(new Error("502"));
  await render(<PlanCard approvalId="ap-1" />);
  expect(await screen.findByText("Couldn't load the plan ·")).toBeTruthy();
  mockGet.mockImplementation(async () => mockApproval.current);
  await fireEvent.press(screen.getByRole("button", { name: "Retry" }));
  expect(await screen.findByRole("button", { name: "Approve plan" })).toBeTruthy();
});

it("shows one card per own start in the DM, newest first", async () => {
  const start = (id: string, createdAt: string) => ({ ideaId: id, title: id, agentId: "g", agentName: "Growth", roomId: "dm-1",
    projectId: `p-${id}`, starterId: "me", starterName: "Me", createdAt,
    plan: { approvalId: `ap-${id}`, status: "pending", title: `Plan: ${id}` } });
  mockListIdeas.mockResolvedValue({ ideas: [], running: [start("old", "2026-10-15T09:00:00Z"), start("new", "2026-10-15T10:00:00Z")] });
  await render(<DmIdeaPlan roomId="dm-1" />);
  await waitFor(() => expect(screen.getAllByRole("button", { name: "Approve plan" })).toHaveLength(2));
  expect([...new Set(mockGet.mock.calls.map(([id]) => id))]).toEqual(["ap-new", "ap-old"]);
});

it("keeps asking /ideas after Change something until a new pending plan arrives", async () => {
  jest.useFakeTimers();
  try {
    mockListIdeas.mockResolvedValue({ ideas: [], running: [{ ideaId: "crm", title: "CRM", agentId: "g", agentName: "Growth",
      roomId: "dm-1", projectId: "p", starterId: "me", starterName: "Me", createdAt: "2026-01-01T00:00:00Z",
      plan: { approvalId: "ap-1", status: "revision_requested", title: "Plan: CRM", decidedAt: new Date().toISOString() } }] });
    await render(<DmIdeaPlan roomId="dm-1" />);
    await waitFor(() => expect(mockListIdeas).toHaveBeenCalledTimes(1));
    await jest.advanceTimersByTimeAsync(10_500);
    await waitFor(() => expect(mockListIdeas).toHaveBeenCalledTimes(2));
  } finally {
    jest.useRealTimers();
  }
});

it("re-reads /ideas right after Change something, so the revision's poll window starts", async () => {
  mockListIdeas.mockResolvedValue({ ideas: [], running: [{ ideaId: "crm", title: "CRM", agentId: "g", agentName: "Growth",
    roomId: "dm-1", projectId: "p", starterId: "me", starterName: "Me", createdAt: "2026-01-01T00:00:00Z",
    plan: { approvalId: "ap-1", status: "pending", title: "Plan: Your CRM set up" } }] });
  await render(<DmIdeaPlan roomId="dm-1" />);
  await fireEvent.press(await screen.findByRole("button", { name: "Change something" }));
  await fireEvent.changeText(screen.getByLabelText("What should change?"), "Smaller first batch");
  const before = mockListIdeas.mock.calls.length;
  await fireEvent.press(screen.getByRole("button", { name: "Send" }));
  await waitFor(() => expect(mockListIdeas.mock.calls.length).toBeGreaterThan(before));
  expect(mockRevise).toHaveBeenCalledWith("ap-1", "Smaller first batch");
});
