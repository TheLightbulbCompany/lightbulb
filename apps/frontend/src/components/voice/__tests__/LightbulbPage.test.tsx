import { render, screen } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";

vi.mock("../TalkOrb", () => ({ TalkOrb: ({ name, compact }: { name: string; compact?: boolean }) => <p>orb:{name}:{compact ? "compact" : "full"}</p> }));
vi.mock("../data", () => ({ nameOf: (c?: { name?: string }) => c?.name || "Lightbulb", useAgentConfig: () => ({ data: { name: "Nova" } }) }));
const dm = vi.hoisted(() => ({ room: { roomId: "dm1", kind: "dm" } as { roomId: string } | null | undefined }));
vi.mock("@/components/huddle/useAssistantDm", () => ({ useAssistantDm: () => dm.room }));
const ws = vi.hoisted(() => ({ activeRoomId: null as string | null, openMessage: vi.fn(), provider: vi.fn() }));
vi.mock("@/components/messaging/MessagingWorkspaceContext", () => ({
  MessagingWorkspaceProvider: ({ children }: { children: React.ReactNode }) => { ws.provider(); return <>{children}</>; },
  useMessagingWorkspace: () => ({ activeRoomId: ws.activeRoomId, openMessage: ws.openMessage }),
}));
vi.mock("@/components/messaging/MessagingWorkspace", () => ({ MessagingConversation: () => <p>room</p> }));
vi.mock("@/components/workspace/InvitePeopleDialog", () => ({ InvitePeopleDialog: () => null }));

import { LightbulbPage } from "../LightbulbPage";

beforeEach(() => { dm.room = { roomId: "dm1" }; ws.activeRoomId = null; });

it("the full orb on top, then the assistant's DM once the provider has it open", () => {
  const { rerender } = render(<LightbulbPage />);
  expect(screen.getByText("orb:Nova:full")).toBeInTheDocument();
  expect(ws.provider).toHaveBeenCalled();
  expect(ws.openMessage).toHaveBeenCalledWith("dm1", null);
  expect(screen.getByRole("status")).toHaveTextContent("Loading chat…");
  ws.activeRoomId = "dm1";
  rerender(<LightbulbPage />);
  const room = screen.getByText("room");
  expect(screen.getByText("orb:Nova:full").compareDocumentPosition(room) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  expect(ws.openMessage).toHaveBeenCalledTimes(1);
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
