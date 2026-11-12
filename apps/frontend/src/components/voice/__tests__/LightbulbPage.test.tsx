import { render, screen } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";

vi.mock("../TalkOrb", () => ({ TalkOrb: ({ name }: { name: string }) => <p>orb:{name}</p> }));
vi.mock("../data", () => ({ LIGHTBULB: "Lightbulb" }));
vi.mock("../AssistantThread", () => ({ AssistantThread: ({ roomId }: { roomId: string }) => <p>thread:{roomId}</p> }));
const dm = vi.hoisted(() => ({ room: { roomId: "dm1" } as { roomId: string } | null | undefined }));
vi.mock("@/components/huddle/useAssistantDm", () => ({ useAssistantDm: () => dm.room }));

import { LightbulbPage } from "../LightbulbPage";

beforeEach(() => { dm.room = { roomId: "dm1" }; });

it("the orb on top, then the assistant's DM", () => {
  render(<LightbulbPage />);
  const thread = screen.getByText("thread:dm1");
  expect(screen.getByText("orb:Lightbulb").compareDocumentPosition(thread) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
});

it("waits while the DM is unknown, and says so when there is none", () => {
  dm.room = undefined;
  const { rerender } = render(<LightbulbPage />);
  expect(screen.getByRole("status")).toHaveTextContent("Loading chat…");
  dm.room = null;
  rerender(<LightbulbPage />);
  expect(screen.getByText(/No conversation yet/)).toBeInTheDocument();
});
