import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import { RoomMessageSearch } from "../RoomMessageSearch";
import type { RoomMessage } from "../types";

const message: RoomMessage = { messageId: "message", roomId: "room", messageSeq: 1, roomRevision: 1, author: { kind: "human", id: "person", displayName: "Alice" }, text: "Launch checklist", mentions: [], replyToMessageId: null, threadRootMessageId: null, createdAt: "2026-09-10T00:00:00Z", runId: null };

it("searches only the current room and identifies partial results and thread replies", async () => {
  const onSelect = vi.fn();
  const onLoad = vi.fn();
  const reply = { ...message, messageId: "reply", messageSeq: 2, text: "Launch is ready", threadRootMessageId: message.messageId };
  const view = render(<RoomMessageSearch roomId="room" messages={[message, reply, { ...message, roomId: "elsewhere", messageId: "foreign", text: "Launch secret" }]} participants={[]} onLoad={onLoad} onSelect={onSelect} loading />);
  await userEvent.click(screen.getByRole("button", { name: "Search this conversation" }));
  expect(onLoad).toHaveBeenCalledOnce();
  expect(screen.getByRole("searchbox", { name: "Search messages" })).toHaveFocus();
  await userEvent.type(screen.getByRole("searchbox"), " LAUNCH ");
  expect(screen.getByRole("status")).toHaveTextContent("Results may be incomplete");
  expect(within(screen.getByRole("list", { name: "Search results" })).getAllByRole("listitem")).toHaveLength(2);
  expect(screen.queryByText("Launch secret")).not.toBeInTheDocument();
  view.rerender(<RoomMessageSearch roomId="room" messages={[message, reply]} participants={[]} onSelect={onSelect} complete />);
  expect(screen.getByRole("status")).toHaveTextContent("2 messages found");
  await userEvent.click(screen.getByRole("button", { name: /Thread reply Launch is ready/ }));
  expect(onSelect).toHaveBeenCalledWith(reply);
});

it("offers recovery instead of claiming no matches across missing history", async () => {
  const onLoad = vi.fn();
  render(<RoomMessageSearch roomId="room" messages={[]} participants={[]} error onLoad={onLoad} onSelect={vi.fn()} />);
  await userEvent.click(screen.getByRole("button", { name: "Search this conversation" }));
  await userEvent.type(screen.getByRole("searchbox"), "missing");
  expect(screen.getByRole("status")).toHaveTextContent("Could not load all messages");
  expect(screen.getByText("No matching messages in the history loaded so far.")).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Load conversation history" }));
  expect(onLoad).toHaveBeenCalledTimes(2);
});
