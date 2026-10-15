import { fireEvent, render, screen, waitFor } from "@testing-library/react-native";
import type { Idea } from "@lightbulb/shared";
import type { ReactNode } from "react";

jest.mock("@clerk/expo", () => ({ useAuth: () => ({ userId: "user-a", orgId: "org-a" }) }), { virtual: true });
// The native sheet is iOS-only; its content is what this test reads.
jest.mock("../../ui/BottomSheet", () => ({
  BottomSheet: ({ visible, children }: { visible: boolean; children: ReactNode }) => (visible ? children : null),
}));
jest.mock("expo-symbols", () => ({ SymbolView: () => null }));

const idea = (id: string, agentId: string, agentName: string): Idea => ({
  id, ownerRole: "growth", kind: "agency", businessTypes: ["any"], title: `Title ${id}`, why: `Why ${id}`,
  deliverables: [`${id} one`, `${id} two`, `${id} three`], ourTurnaround: "about 2 days", yourPart: "about 5 minutes",
  agentId, agentName,
});
const mockClient = {
  listIdeas: jest.fn(async () => ({
    ideas: [idea("a", "eng", "Engineering"), idea("b", "growth", "Growth"), idea("r", "fin", "Finance")],
    running: [{ ideaId: "r", title: "Title r", agentId: "fin", agentName: "Finance", roomId: "room-r", projectId: "p",
      starterId: "user-a", starterName: "A", createdAt: null }],
  })),
  startIdea: jest.fn(async () => ({ roomId: "room-b", projectId: "p-b" })),
  dismissIdea: jest.fn(async () => {}),
};
jest.mock("../../../providers/live-events", () => ({ useLiveEvents: () => ({ messaging: mockClient }) }));

import { IdeasSheet } from "../IdeasSheet";

const renderSheet = async (onStarted = jest.fn()) => {
  await render(<IdeasSheet visible onClose={jest.fn()} agentId="growth" onStarted={onStarted} />);
  return onStarted;
};

it("lists ideas as cards, the DM agent's first, running ones left out, with no price", async () => {
  await renderSheet();
  expect(await screen.findByText("Title b")).toBeTruthy();
  const titles = screen.getAllByText(/^Title /).map((node) => node.props.children);
  expect(titles).toEqual(["Title b", "Title a"]);
  expect(screen.getAllByText("Your team: about 2 days · You: about 5 minutes")).toHaveLength(2);
  expect(screen.queryByText(/\$/)).toBeNull();
});

it("Start calls the API and hands back the DM", async () => {
  const onStarted = await renderSheet();
  await fireEvent.press(await screen.findByRole("button", { name: "Start: Title a" }));
  await waitFor(() => expect(onStarted).toHaveBeenCalledWith("room-b"));
  expect(mockClient.startIdea).toHaveBeenCalledWith("a");
});
