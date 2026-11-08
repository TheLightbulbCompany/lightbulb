import { act, render, screen } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";

vi.mock("../TalkOrb", () => ({ TalkOrb: ({ name, compact }: { name: string; compact?: boolean }) => <p>orb:{name}:{compact ? "compact" : "full"}</p> }));
vi.mock("../data", () => ({ LIGHTBULB: "Lightbulb" }));
const dm = vi.hoisted(() => ({ room: { roomId: "dm1", kind: "dm" } as { roomId: string } | null | undefined }));
vi.mock("@/components/huddle/useAssistantDm", () => ({ useAssistantDm: () => dm.room }));
const ws = vi.hoisted(() => ({ activeRoomId: null as string | null, openMessage: vi.fn(), provider: vi.fn() }));
vi.mock("@/components/messaging/MessagingWorkspaceContext", () => ({
  MessagingWorkspaceProvider: ({ children }: { children: React.ReactNode }) => { ws.provider(); return <>{children}</>; },
  useMessagingWorkspace: () => ({ activeRoomId: ws.activeRoomId, openMessage: ws.openMessage }),
}));
vi.mock("@/components/messaging/MessagingWorkspace", () => ({ MessagingConversation: ({ voiceControls, oneToOne }: { voiceControls?: boolean; oneToOne?: boolean }) => <p data-voice={String(voiceControls)} data-one={String(oneToOne)}>room</p> }));
vi.mock("@/components/workspace/RightPane", () => ({ RightPane: ({ pane }: { pane: { component: string; params: Record<string, string> } }) => <aside aria-label="pane" data-kind={pane.component} data-id={pane.params.id} /> }));
vi.mock("@/components/workspace/InvitePeopleDialog", () => ({ InvitePeopleDialog: () => null }));

import { LightbulbPage } from "../LightbulbPage";
import { _resetControllerForTests, getWorkspaceSnapshot, openPanel } from "@/components/workspace/workspaceController";

beforeEach(() => { dm.room = { roomId: "dm1" }; ws.activeRoomId = null; _resetControllerForTests(); });

it("the full orb on top, then the assistant's DM once the provider has it open", () => {
  const { rerender } = render(<LightbulbPage />);
  expect(screen.getByText("orb:Lightbulb:full")).toBeInTheDocument();
  expect(ws.provider).toHaveBeenCalled();
  expect(ws.openMessage).toHaveBeenCalledWith("dm1", null);
  expect(screen.getByRole("status")).toHaveTextContent("Loading chat…");
  ws.activeRoomId = "dm1";
  rerender(<LightbulbPage />);
  const room = screen.getByText("room");
  expect(screen.getByText("orb:Lightbulb:full").compareDocumentPosition(room) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  expect(ws.openMessage).toHaveBeenCalledTimes(1);
  // The orb is the only voice control: the room header drops its Live strip and Go live here.
  expect(room).toHaveAttribute("data-voice", "false");
  // The member's own DM: no Members tab, Add people or invites (RoomPanel).
  expect(room).toHaveAttribute("data-one", "true");
});

it("what the room opens in the right pane (a file, a result, a task) opens beside it here, and closes with the page", () => {
  ws.activeRoomId = "dm1";
  const { unmount } = render(<LightbulbPage />);
  expect(screen.queryByRole("complementary", { name: "pane" })).toBeNull();
  // RoomImageAttachments' tile: no shell is mounted, and it is no longer a dead click.
  act(() => openPanel("artifact", { src: "attachment", room: "dm1", id: "a1", name: "x.png", type: "image/png" }, { pushUrl: true }));
  expect(screen.getByRole("complementary", { name: "pane" })).toHaveAttribute("data-kind", "artifact");
  expect(screen.getByRole("complementary", { name: "pane" })).toHaveAttribute("data-id", "a1");
  unmount();
  expect(getWorkspaceSnapshot().pane).toBeNull();
});

it("waits while the DM is unknown, and shows the provider's own pick when there is none", () => {
  dm.room = undefined;
  const { rerender } = render(<LightbulbPage />);
  expect(screen.getByRole("status")).toHaveTextContent("Loading chat…");
  expect(ws.openMessage).not.toHaveBeenCalled();
  dm.room = null;
  rerender(<LightbulbPage />);
  expect(screen.getByText("room")).toBeInTheDocument();
});
