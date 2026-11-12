import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";

const rooms = vi.hoisted(() => ({
  activeRoomId: "dm1" as string | null,
  messages: [] as unknown[],
  optimisticMessages: [] as unknown[],
  runs: [] as { roomId: string; state: string }[],
  selectRoom: vi.fn(),
  sendMessage: vi.fn(),
}));
vi.mock("@/hooks/useRooms", () => ({ useRooms: () => rooms }));

import { AssistantThread } from "../AssistantThread";

beforeEach(() => {
  rooms.activeRoomId = "dm1";
  rooms.selectRoom.mockReset();
  rooms.sendMessage.mockReset().mockResolvedValue(undefined);
  rooms.messages = [
    { messageId: "m1", text: "Book Sam for **Friday**", author: { kind: "human", id: "u1", displayName: "Ana" } },
    { messageId: "m2", text: "Done.", author: { kind: "agent", id: "a1", displayName: "Lightbulb" } },
  ];
  rooms.optimisticMessages = [{ clientMessageId: "c1", text: "Thanks" }];
  rooms.runs = [];
});

it("shows one replying row while a run in this room is running, and none otherwise", () => {
  rooms.runs = [{ roomId: "other", state: "running" }, { roomId: "dm1", state: "completed" }];
  const { rerender } = render(<AssistantThread roomId="dm1" />);
  expect(screen.queryByText("Lightbulb is replying…")).toBeNull();
  rooms.runs = [{ roomId: "dm1", state: "running" }];
  rerender(<AssistantThread roomId="dm1" />);
  expect(screen.getByText("Lightbulb is replying…")).toBeInTheDocument();
});

it("a retry of a failed send with the same text reuses its clientMessageId; new text gets a new one", async () => {
  rooms.sendMessage.mockRejectedValueOnce(new Error("timeout"));
  render(<AssistantThread roomId="dm1" />);
  const box = screen.getByRole("textbox", { name: "Message" });
  fireEvent.change(box, { target: { value: "Cancel Tuesday" } });
  fireEvent.keyDown(box, { key: "Enter" });
  expect(await screen.findByRole("alert")).toBeInTheDocument();
  expect(box).toHaveValue("Cancel Tuesday");
  fireEvent.keyDown(box, { key: "Enter" });
  await waitFor(() => expect(rooms.sendMessage).toHaveBeenCalledTimes(2));
  const [first, second] = rooms.sendMessage.mock.calls.map(([req]) => req.clientMessageId);
  expect(second).toBe(first);
  fireEvent.change(box, { target: { value: "Something else" } });
  fireEvent.keyDown(box, { key: "Enter" });
  await waitFor(() => expect(rooms.sendMessage).toHaveBeenCalledTimes(3));
  expect(rooms.sendMessage.mock.calls[2][0].clientMessageId).not.toBe(first);
});

it("opens the DM and renders its messages, markdown and all", () => {
  render(<AssistantThread roomId="dm1" />);
  expect(rooms.selectRoom).toHaveBeenCalledWith("dm1");
  expect(screen.getByText("Friday").tagName).toBe("STRONG");
  expect(screen.getByText("Done.")).toBeInTheDocument();
  expect(screen.getByText("Lightbulb")).toBeInTheDocument();
  expect(screen.getByText("Thanks").closest("li")).toHaveAttribute("data-pending");
});

it("waits until the hook has the DM open", () => {
  rooms.activeRoomId = "other";
  render(<AssistantThread roomId="dm1" />);
  expect(screen.getByRole("status")).toHaveTextContent("Loading chat…");
});

it("Enter sends the text through the hook and empties the box; Shift+Enter does not", () => {
  render(<AssistantThread roomId="dm1" />);
  const box = screen.getByRole("textbox", { name: "Message" });
  fireEvent.change(box, { target: { value: "  Move it to 3pm  " } });
  fireEvent.keyDown(box, { key: "Enter", shiftKey: true });
  expect(rooms.sendMessage).not.toHaveBeenCalled();
  fireEvent.keyDown(box, { key: "Enter" });
  expect(rooms.sendMessage).toHaveBeenCalledWith(expect.objectContaining({ text: "Move it to 3pm", mentions: [], replyToMessageId: null, threadRootMessageId: null }));
  expect(box).toHaveValue("");
});
