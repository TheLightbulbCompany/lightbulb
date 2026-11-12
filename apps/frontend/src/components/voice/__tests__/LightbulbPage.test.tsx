import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";

vi.mock("../TalkOrb", () => ({ TalkOrb: ({ name }: { name: string }) => <p>orb:{name}</p> }));
const chats = vi.hoisted(() => ({ data: undefined as { roomId: string }[] | undefined }));
vi.mock("../data", () => ({ LIGHTBULB: "Lightbulb", useNewChat: () => ({ start: vi.fn(), state: "idle" }), useTextChats: () => chats }));
vi.mock("../AssistantThread", () => ({ AssistantThread: ({ roomId }: { roomId: string }) => <p>thread:{roomId}</p> }));
const dm = vi.hoisted(() => ({ room: { roomId: "dm1" } as { roomId: string } | null | undefined }));
vi.mock("@/components/huddle/useAssistantDm", () => ({ useAssistantDm: () => dm.room }));

const api = vi.hoisted(() => ({ get: vi.fn() }));
vi.mock("@/lib/api", () => ({ useApi: () => api }));
import { SWRConfig } from "swr";
import { ApiError } from "@lightbulb/shared";
import { LightbulbPage } from "../LightbulbPage";
const fresh = (ui: React.ReactElement) => render(<SWRConfig value={{ provider: () => new Map() }}>{ui}</SWRConfig>);

beforeEach(() => { dm.room = { roomId: "dm1" }; chats.data = undefined; api.get.mockReset(); });

it("the orb on top, then the assistant's DM", () => {
  render(<LightbulbPage />);
  const thread = screen.getByText("thread:dm1");
  expect(screen.getByText("orb:Lightbulb").compareDocumentPosition(thread) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
});

it("?room= opens that chat instead of the DM, with New chat beside the orb", () => {
  render(<LightbulbPage room="g7" />);
  expect(screen.getByText("thread:g7")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /New chat/ })).toBeInTheDocument();
});

it("a ?room= the list lacks is asked for by id: it opens when the room exists, Chat not found only on its 404", async () => {
  chats.data = [{ roomId: "dm1" }];
  api.get.mockResolvedValueOnce({ roomId: "new1" }); // just made: the list's index hasn't caught up yet
  fresh(<LightbulbPage room="new1" />);
  expect(screen.getByText("thread:new1")).toBeInTheDocument();
  await waitFor(() => expect(api.get).toHaveBeenCalledWith("/messaging/rooms/new1"));
  expect(screen.queryByRole("alert")).toBeNull();
  expect(screen.getByText("thread:new1")).toBeInTheDocument();
});

it("Chat not found, with no composer, when the room itself answers 404", async () => {
  chats.data = [{ roomId: "dm1" }];
  api.get.mockRejectedValueOnce(new ApiError(404, {}));
  fresh(<LightbulbPage room="nope" />);
  expect(await screen.findByRole("alert")).toHaveTextContent("Chat not found");
  expect(screen.getByRole("link", { name: "Back to Home" })).toHaveAttribute("href", "/voice");
  expect(screen.queryByText(/^thread:/)).toBeNull();
});

it("waits while the DM is unknown, and says so when there is none", () => {
  dm.room = undefined;
  const { rerender } = render(<LightbulbPage />);
  expect(screen.getByRole("status")).toHaveTextContent("Loading chat…");
  dm.room = null;
  rerender(<LightbulbPage />);
  expect(screen.getByText(/No conversation yet/)).toBeInTheDocument();
});
