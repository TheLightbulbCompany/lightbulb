import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import type { RoomSummary } from "@lightbulb/shared";

const api = vi.hoisted(() => ({ post: vi.fn(), get: vi.fn() }));
vi.mock("@/lib/api", () => ({ useApi: () => api }));
const nav = vi.hoisted(() => ({ push: vi.fn() }));
vi.mock("next/navigation", () => ({ useRouter: () => nav }));

import { textChats } from "../data";
import { NewChatButton } from "../LightbulbPage";

const room = (r: Partial<RoomSummary>) => ({ name: null, kind: "dm", members: [], lastMessageAt: null, updatedAt: "2026-11-01T00:00:00Z", ...r }) as RoomSummary;
const me = { kind: "human", id: "u1" }, bot = { kind: "agent", id: "a1" };

beforeEach(() => { api.post.mockReset(); nav.push.mockReset(); });

it("text chats: my DM and group chats with only me and agents, newest first, titled by name or New chat", () => {
  const chats = textChats([
    room({ roomId: "dm1", members: [me, bot] as RoomSummary["members"], lastMessageAt: "2026-11-02T00:00:00Z" }),
    room({ roomId: "g1", kind: "group-dm", members: [me, bot] as RoomSummary["members"], lastMessageAt: "2026-11-05T00:00:00Z" }),
    room({ roomId: "g2", kind: "group-dm", name: "Spring promo", members: [me, bot] as RoomSummary["members"], lastMessageAt: "2026-11-04T00:00:00Z" }),
    room({ roomId: "people", kind: "group-dm", members: [me, bot, { kind: "human", id: "u2" }] as RoomSummary["members"] }),
    room({ roomId: "ch", kind: "channel", members: [me, bot] as RoomSummary["members"] }),
    room({ roomId: "noagent", members: [me, { kind: "human", id: "u2" }] as RoomSummary["members"] }),
  ], "u1");
  expect(chats.map((c) => [c.roomId, c.title])).toEqual([["g1", "New chat"], ["g2", "Spring promo"], ["dm1", "You and Lightbulb"]]);
});

it("New chat creates a group chat (the backend seats Lightbulb) and opens it", async () => {
  api.post.mockResolvedValue({ roomId: "new1" });
  render(<NewChatButton />);
  fireEvent.click(screen.getByRole("button", { name: /New chat/ }));
  await waitFor(() => expect(nav.push).toHaveBeenCalledWith("/voice/lightbulb?room=new1"));
  expect(api.post).toHaveBeenCalledWith("/messaging/rooms", expect.objectContaining({ kind: "group-dm", name: null, members: [] }));
});

it("a failed New chat says so and can be tried again", async () => {
  api.post.mockRejectedValue(new Error("down"));
  render(<NewChatButton />);
  fireEvent.click(screen.getByRole("button", { name: /New chat/ }));
  expect(await screen.findByRole("button", { name: /Try again/ })).toBeEnabled();
  expect(nav.push).not.toHaveBeenCalled();
});
