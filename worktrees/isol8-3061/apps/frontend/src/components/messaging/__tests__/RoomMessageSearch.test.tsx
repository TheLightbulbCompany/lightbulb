import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";

const capture = vi.fn();
vi.mock("@/lib/analytics", () => ({ capture: (...args: unknown[]) => capture(...args) }));

import { RoomMessageSearch } from "../RoomMessageSearch";
import type { RoomMessage } from "../types";

const message: RoomMessage = { messageId: "message", roomId: "room", messageSeq: 1, roomRevision: 1, author: { kind: "human", id: "person", displayName: "Alice" }, text: "Launch checklist", mentions: [], replyToMessageId: null, threadRootMessageId: null, createdAt: "2026-09-10T00:00:00Z", runId: null };

beforeEach(() => capture.mockClear());

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
  expect(capture).toHaveBeenCalledWith("message_search_used", { result_count_bucket: "1-5" });
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

it("names the failure when the server gives a history error reason", async () => {
  render(<RoomMessageSearch roomId="room" messages={[]} participants={[]} error errorMessage="History is temporarily unavailable" onSelect={vi.fn()} />);
  await userEvent.click(screen.getByRole("button", { name: "Search this conversation" }));
  expect(screen.getByRole("status")).toHaveTextContent("Could not load all messages: History is temporarily unavailable");
});

it("captures message_search_used with a bucketed result count once the search is closed", async () => {
  render(<RoomMessageSearch roomId="room" messages={[message]} participants={[]} onSelect={vi.fn()} complete />);
  await userEvent.click(screen.getByRole("button", { name: "Search this conversation" }));
  await userEvent.type(screen.getByRole("searchbox"), "nothing matches this");
  await userEvent.keyboard("{Escape}");
  expect(capture).toHaveBeenCalledWith("message_search_used", { result_count_bucket: "0" });
});

it("buckets 6+ once results run past five", async () => {
  const many = Array.from({ length: 6 }, (_, i) => ({ ...message, messageId: `m${i}`, text: "Launch item" }));
  render(<RoomMessageSearch roomId="room" messages={many} participants={[]} onSelect={vi.fn()} complete />);
  await userEvent.click(screen.getByRole("button", { name: "Search this conversation" }));
  await userEvent.type(screen.getByRole("searchbox"), "launch");
  await userEvent.keyboard("{Escape}");
  expect(capture).toHaveBeenCalledWith("message_search_used", { result_count_bucket: "6+" });
});

it("does not capture message_search_used when the dialog closes without a search", async () => {
  render(<RoomMessageSearch roomId="room" messages={[message]} participants={[]} onSelect={vi.fn()} complete />);
  await userEvent.click(screen.getByRole("button", { name: "Search this conversation" }));
  await userEvent.keyboard("{Escape}");
  expect(capture).not.toHaveBeenCalled();
});
