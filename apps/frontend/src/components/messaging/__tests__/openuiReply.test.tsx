import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import type { CanonicalRoomMessage } from "@lightbulb/shared";
import { defaultUrlTransform } from "react-markdown";
import { RoomMessageItem } from "../RoomMessageItem";
import { roomMarkdown } from "../roomMarkdown";

vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));

const program = "```openui-lang\nroot = Stack([b])\nb = Buttons([Button(\"Studio\"), Button(\"Starter\")])\n```";
const agentMessage = (over: Partial<CanonicalRoomMessage> = {}): CanonicalRoomMessage => ({
  messageId: "card", roomId: "room", messageSeq: 3, roomRevision: 1, author: { kind: "agent", id: "agent-1", displayName: "Friday" },
  text: program, mentions: [], replyToMessageId: null, threadRootMessageId: null, createdAt: "2026-10-16T10:00:00Z", runId: null, ...over,
});
const itemProps = (message: CanonicalRoomMessage, extra: Record<string, unknown> = {}) => ({
  id: "m-card", message, text: message.text, currentUserId: "user-1", actionable: true, inThread: false, collapsed: false,
  replyCount: 0, highlighted: false, mentionsViewer: false, markdown: roomMarkdown, urlTransform: defaultUrlTransform,
  quickReactions: [], reactionPending: false, reactionError: false, ...extra,
});

it("a press posts the label as the person, mentioning the agent, in reply to the card", async () => {
  const onCardReply = vi.fn().mockResolvedValue(undefined);
  render(<RoomMessageItem {...itemProps(agentMessage(), { onCardReply, cardAnswer: null })} />);
  await userEvent.click(await screen.findByRole("button", { name: "Studio" }));
  expect(onCardReply).toHaveBeenCalledWith({ text: "Studio", agentId: "agent-1", messageId: "card", threadRootMessageId: null, clientMessageId: expect.any(String) });
});

it("thread cards answer in the thread", async () => {
  const onCardReply = vi.fn().mockResolvedValue(undefined);
  render(<RoomMessageItem {...itemProps(agentMessage({ threadRootMessageId: "root-7" }), { onCardReply, cardAnswer: null, inThread: true })} />);
  await userEvent.click(await screen.findByRole("button", { name: "Starter" }));
  expect(onCardReply).toHaveBeenCalledWith(expect.objectContaining({ threadRootMessageId: "root-7", messageId: "card" }));
});

it("one press, one message", async () => {
  let resolve!: () => void;
  const onCardReply = vi.fn(() => new Promise<void>((r) => { resolve = r; }));
  render(<RoomMessageItem {...itemProps(agentMessage(), { onCardReply, cardAnswer: null })} />);
  const button = await screen.findByRole("button", { name: "Studio" });
  await userEvent.click(button);
  await userEvent.click(button);
  expect(onCardReply).toHaveBeenCalledTimes(1);
  await act(async () => { resolve(); });
});

it("an answered card shows the pick and disables its controls", async () => {
  render(<RoomMessageItem {...itemProps(agentMessage(), { onCardReply: vi.fn(), cardAnswer: "Studio" })} />);
  expect(await screen.findByRole("button", { name: "Studio" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Studio" })).toHaveAttribute("aria-pressed", "true");
  expect(screen.getByRole("button", { name: "Starter" })).toBeDisabled();
});

it("no reply function means disabled", async () => {
  render(<RoomMessageItem {...itemProps(agentMessage(), { cardAnswer: null })} />);
  expect(await screen.findByRole("button", { name: "Studio" })).toBeDisabled();
});

it("a failed send unlocks the card and says so", async () => {
  const onCardReply = vi.fn().mockRejectedValue(new Error("offline"));
  render(<RoomMessageItem {...itemProps(agentMessage(), { onCardReply, cardAnswer: null })} />);
  await userEvent.click(await screen.findByRole("button", { name: "Studio" }));
  expect(await screen.findByRole("alert")).toHaveTextContent(/try again/i);
  expect(screen.getByRole("button", { name: "Studio" })).not.toBeDisabled();
  expect(onCardReply).toHaveBeenCalledTimes(1);
  await userEvent.click(screen.getByRole("button", { name: "Studio" }));
  expect(onCardReply).toHaveBeenCalledTimes(2);
});

it("a retry reuses the client id", async () => {
  const onCardReply = vi.fn().mockRejectedValueOnce(new Error("timeout")).mockResolvedValueOnce(undefined);
  render(<RoomMessageItem {...itemProps(agentMessage(), { onCardReply, cardAnswer: null })} />);
  await userEvent.click(await screen.findByRole("button", { name: "Studio" }));
  await screen.findByRole("alert");
  await userEvent.click(screen.getByRole("button", { name: "Studio" }));
  expect(onCardReply).toHaveBeenCalledTimes(2);
  expect(onCardReply.mock.calls[1][0].clientMessageId).toBe(onCardReply.mock.calls[0][0].clientMessageId);
});
