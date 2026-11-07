import { fireEvent, render, screen, waitFor } from "@testing-library/react-native";
import type { Idea } from "@lightbulb/shared";
import type { ReactNode } from "react";

let mockOrg = "org-a"; // a fresh org gives a test its own /ideas cache entry
jest.mock("@clerk/expo", () => ({ useAuth: () => ({ userId: "user-a", orgId: mockOrg }) }));
// The native sheet is iOS-only; its content is what this test reads.
jest.mock("../../ui/BottomSheet", () => ({
  BottomSheet: ({ visible, children }: { visible: boolean; children: ReactNode }) => (visible ? children : null),
}));
jest.mock("expo-symbols", () => ({ SymbolView: () => null }));

const idea = (id: string, agentId: string, agentName: string): Idea => ({
  id, ownerRole: "growth", emoji: `e-${id}`, kind: "agency", size: "project", businessTypes: ["any"], title: `Title ${id}`, why: `Why ${id}`,
  deliverables: [`${id} one`, `${id} two`, `${id} three`], ourTurnaround: "about 2 days", yourPart: "about 5 minutes",
  agentId, agentName,
});
const mockClient = {
  listIdeas: jest.fn(async () => ({
    ideas: [idea("a", "eng", "Engineering"), idea("b", "growth", "Growth"), idea("t", "fin", "Finance")],
  })),
  startIdea: jest.fn(async () => ({ roomId: "room-b", projectId: "p-b" })),
  dismissIdea: jest.fn(async () => {}),
};
jest.mock("../../../providers/live-events", () => ({ useLiveEvents: () => ({ messaging: mockClient }) }));

import { ActionSheetIOS } from "react-native";
import { act } from "@testing-library/react-native";
import { IdeasSheet } from "../IdeasSheet";

const renderSheet = async (onStarted = jest.fn()) => {
  await render(<IdeasSheet visible onClose={jest.fn()} agentId="growth" onStarted={onStarted} />);
  return onStarted;
};

beforeEach(() => jest.clearAllMocks());

it("lists every idea the server sends as one row (the DM agent's first), with no Running section, price or Start", async () => {
  await renderSheet();
  expect(await screen.findByRole("button", { name: "Title b" })).toBeTruthy();
  const rows = screen.getAllByRole("button").map((node) => node.props.accessibilityLabel);
  expect(rows).toEqual(["Title b", "Title a", "Title t"]);
  expect(screen.queryByText("Running")).toBeNull();
  expect(screen.getByRole("header", { name: "Grow" })).toBeTruthy(); // all three are growth ideas: one section
  expect(screen.getByText("Why a")).toBeTruthy();
  expect(screen.queryByText(/\$|Start$|can do this for you/)).toBeNull();
});

it("a row opens the detail: what's included (read-only), the effort, Let's do it hands back the DM", async () => {
  const onStarted = await renderSheet();
  await fireEvent.press(await screen.findByRole("button", { name: "Title a" }));
  expect(screen.getByText("What you get")).toBeTruthy();
  expect(screen.getByText("a one")).toBeTruthy();
  expect(screen.getByText("Engineering takes this · about 2 days")).toBeTruthy();
  expect(screen.getByText("Your part: about 5 minutes")).toBeTruthy();
  await fireEvent.press(screen.getByRole("button", { name: "Let's do it" }));
  await waitFor(() => expect(onStarted).toHaveBeenCalledWith("room-b"));
  expect(mockClient.startIdea).toHaveBeenCalledWith("a");
});

it("… → Not interested hides the idea", async () => {
  const sheet = jest.spyOn(ActionSheetIOS, "showActionSheetWithOptions").mockImplementation(() => {});
  await renderSheet();
  await fireEvent.press(await screen.findByRole("button", { name: "Title a" }));
  await fireEvent.press(screen.getByRole("button", { name: "More options" }));
  const [options, callback] = sheet.mock.calls.at(-1)!;
  expect(options.options).toEqual(["Not interested", "Cancel"]);
  await act(async () => { callback(0); });
  expect(mockClient.dismissIdea).toHaveBeenCalledWith("a");
});

it("shows the server's reason when Let's do it fails", async () => {
  mockClient.startIdea.mockRejectedValueOnce(new Error("Work did not answer. Try Start again in a moment."));
  const onStarted = await renderSheet();
  await fireEvent.press(await screen.findByRole("button", { name: "Title b" }));
  await fireEvent.press(screen.getByRole("button", { name: "Let's do it" }));
  expect(await screen.findByText("Work did not answer. Try Start again in a moment.")).toBeTruthy();
  expect(onStarted).not.toHaveBeenCalled();
});

it("puts an agent's suggestion under For you, and its detail has no turnaround or your part", async () => {
  mockOrg = "org-suggestions";
  mockClient.listIdeas.mockResolvedValueOnce({
    ideas: [{ ...idea("s1", "growth", "Growth"), suggested: true, ourTurnaround: undefined, yourPart: undefined }],
  } as never);
  await render(<IdeasSheet visible onClose={jest.fn()} onStarted={jest.fn()} />);
  expect(await screen.findByText("For you")).toBeTruthy();
  await fireEvent.press(screen.getByRole("button", { name: "Title s1" }));
  expect(screen.getByText("Growth takes this")).toBeTruthy();
  expect(screen.queryByText(/Your part:/)).toBeNull();
});
