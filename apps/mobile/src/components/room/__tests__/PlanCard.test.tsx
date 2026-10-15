import { fireEvent, render, screen, waitFor } from "@testing-library/react-native";

jest.mock("@clerk/expo", () => ({ useAuth: () => ({ userId: "me", orgId: "org", getToken: async () => "t" }) }), { virtual: true });
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
