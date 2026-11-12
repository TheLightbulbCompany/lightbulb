import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeAll, expect, it, vi } from "vitest";
import { ApiError, type CanonicalRoomMessage } from "@lightbulb/shared";
import { defaultUrlTransform } from "react-markdown";
import { RoomMessageItem } from "../RoomMessageItem";
import { roomMarkdown } from "../roomMarkdown";

// The card is a lazy chunk; the first mount in a file otherwise pays the import inside findByRole's 1 s budget.
beforeAll(() => import("@/components/voice/openui/OpenUICard"));

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

it("a feed post's fence stays code", () => {
  render(<RoomMessageItem {...itemProps(agentMessage(), { onCardReply: vi.fn(), cardAnswer: null, cardsEnabled: false })} />);
  expect(screen.getByText(/root = Stack/).closest("pre")).not.toBeNull();
  expect(screen.queryByRole("button", { name: "Studio" })).toBeNull();
});

it("two fences in one reply share one answer", async () => {
  const onCardReply = vi.fn().mockResolvedValue(undefined);
  const two = program + "\n\n```openui-lang\nroot = Stack([b])\nb = Buttons([Button(\"Alpha\"), Button(\"Beta\")])\n```";
  render(<RoomMessageItem {...itemProps(agentMessage({ text: two }), { onCardReply, cardAnswer: null })} />);
  await userEvent.click(await screen.findByRole("button", { name: "Studio" }));
  expect(await screen.findByRole("button", { name: "Alpha" })).toBeDisabled();
  await userEvent.click(screen.getByRole("button", { name: "Alpha" }));
  expect(onCardReply).toHaveBeenCalledTimes(1);
});

it("a send with no answer pins its reply: the other option waits, the retry replays the same id and text", async () => {
  const onCardReply = vi.fn().mockRejectedValueOnce(new Error("timeout")).mockResolvedValueOnce(undefined);
  render(<RoomMessageItem {...itemProps(agentMessage(), { onCardReply, cardAnswer: null })} />);
  await userEvent.click(await screen.findByRole("button", { name: "Studio" }));
  await screen.findByRole("alert");
  expect(screen.getByRole("button", { name: "Starter" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Studio" })).not.toBeDisabled();
  await userEvent.click(screen.getByRole("button", { name: "Studio" }));
  expect(onCardReply).toHaveBeenCalledTimes(2);
  expect(onCardReply.mock.calls[1][0]).toMatchObject({ text: "Studio", clientMessageId: onCardReply.mock.calls[0][0].clientMessageId });
  expect(await screen.findByRole("button", { name: "Studio" })).toHaveAttribute("aria-pressed", "true");
});

it("a send the server rejected frees the card: another choice goes out with a fresh id", async () => {
  const onCardReply = vi.fn().mockRejectedValueOnce(new ApiError(400, {}, "bad")).mockResolvedValueOnce(undefined);
  render(<RoomMessageItem {...itemProps(agentMessage(), { onCardReply, cardAnswer: null })} />);
  await userEvent.click(await screen.findByRole("button", { name: "Studio" }));
  await screen.findByRole("alert");
  expect(screen.getByRole("button", { name: "Starter" })).not.toBeDisabled();
  await userEvent.click(screen.getByRole("button", { name: "Starter" }));
  expect(onCardReply).toHaveBeenCalledTimes(2);
  expect(onCardReply.mock.calls[1][0].text).toBe("Starter");
  expect(onCardReply.mock.calls[1][0].clientMessageId).not.toBe(onCardReply.mock.calls[0][0].clientMessageId);
});

it("a canonical answer clears whatever the card still held of the send; deleting the reply reopens it", async () => {
  const onCardReply = vi.fn().mockRejectedValueOnce(new Error("timeout"));
  const view = render(<RoomMessageItem {...itemProps(agentMessage(), { onCardReply, cardAnswer: null })} />);
  await userEvent.click(await screen.findByRole("button", { name: "Studio" }));
  await screen.findByRole("alert");
  expect(screen.getByRole("button", { name: "Starter" })).toBeDisabled();
  view.rerender(<RoomMessageItem {...itemProps(agentMessage(), { onCardReply, cardAnswer: "Studio" })} />);
  await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
  expect(screen.getByRole("button", { name: "Studio" })).toHaveAttribute("aria-pressed", "true");
  view.rerender(<RoomMessageItem {...itemProps(agentMessage(), { onCardReply, cardAnswer: null })} />);
  await waitFor(() => expect(screen.getByRole("button", { name: "Starter" })).not.toBeDisabled());
  expect(screen.getByRole("button", { name: "Studio" })).not.toBeDisabled();
});

it("a POST that settles after the canonical answer landed writes nothing back", async () => {
  let finish: () => void = () => {};
  const onCardReply = vi.fn(() => new Promise<void>((resolve) => { finish = resolve; }));
  const view = render(<RoomMessageItem {...itemProps(agentMessage(), { onCardReply, cardAnswer: null })} />);
  await userEvent.click(await screen.findByRole("button", { name: "Studio" }));
  view.rerender(<RoomMessageItem {...itemProps(agentMessage(), { onCardReply, cardAnswer: "Studio" })} />);
  await act(async () => { finish(); });
  view.rerender(<RoomMessageItem {...itemProps(agentMessage(), { onCardReply, cardAnswer: null })} />);
  await waitFor(() => expect(screen.getByRole("button", { name: "Starter" })).not.toBeDisabled());
  expect(screen.getByRole("button", { name: "Studio" })).not.toBeDisabled();
});
