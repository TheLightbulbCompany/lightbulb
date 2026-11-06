import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import type { CanonicalRoomMessage } from "@lightbulb/shared";
import { DropdownMenuItem } from "@/components/ui/dropdown-menu";
import { RoomMessageContent } from "../RoomMessageContent";

const message: CanonicalRoomMessage = { messageId: "one", roomId: "room", messageSeq: 1, roomRevision: 1,
  author: { kind: "human", id: "me", displayName: "Me" }, text: "Original", mentions: [], replyToMessageId: null,
  threadRootMessageId: null, createdAt: "2026-09-10T00:00:00Z", runId: null };

it("edits only an own human message and reuses the operation on an uncertain retry", async () => {
  const user = userEvent.setup();
  let reject!: (error: Error) => void;
  const update = vi.fn(() => new Promise<void>((_, fail) => { reject = fail; }));
  render(<RoomMessageContent message={message} currentUserId="me" actionable onUpdate={update}>Original</RoomMessageContent>);
  await user.click(screen.getByRole("button", { name: "Message actions" }));
  await user.click(screen.getByRole("menuitem", { name: "Edit message" }));
  const input = screen.getByRole("textbox", { name: "Edit message" });
  expect(input).toHaveFocus();
  await user.clear(input);
  await user.type(input, "Corrected");
  await user.click(screen.getByRole("button", { name: "Save changes" }));
  expect(screen.getByRole("button", { name: "Saving…" })).toBeDisabled();
  await act(async () => reject(new Error("Connection lost. Try again.")));
  expect(screen.getByRole("alert")).toHaveTextContent("Connection lost");
  update.mockResolvedValue(undefined);
  await user.click(screen.getByRole("button", { name: "Save changes" }));
  expect(update.mock.calls[1]).toEqual(update.mock.calls[0]);
  expect(update).toHaveBeenCalledWith("one", { operationId: expect.any(String), expectedRevision: 0, text: "Corrected" });
});

it("confirms deletion with Cancel focused, and never renders deleted content", async () => {
  const user = userEvent.setup();
  const update = vi.fn().mockResolvedValue(undefined);
  const { rerender } = render(<RoomMessageContent message={message} currentUserId="me" actionable onUpdate={update}>Original</RoomMessageContent>);
  await user.click(screen.getByRole("button", { name: "Message actions" }));
  await user.click(screen.getByRole("menuitem", { name: "Delete message" }));
  const dialog = screen.getByRole("dialog", { name: "Delete message?" });
  expect(within(dialog).getByRole("button", { name: "Cancel" })).toHaveFocus();
  expect(dialog).toHaveTextContent("does not change tasks already sent to specialists");
  await user.click(within(dialog).getByRole("button", { name: "Delete message" }));
  expect(update).toHaveBeenCalledWith("one", expect.objectContaining({ expectedRevision: 0, text: null }));
  rerender(<RoomMessageContent message={{ ...message, deletedAt: "now", messageRevision: 1 }} currentUserId="me" actionable onUpdate={update}>Original</RoomMessageContent>);
  expect(screen.getByText("Message deleted")).toBeInTheDocument();
  expect(screen.queryByText("Original")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Message actions" })).not.toBeInTheDocument();
});

it.each(["human", "agent", "system"] as const)("does not offer edit/delete for another %s author", (kind) => {
  render(<RoomMessageContent message={{ ...message, author: { ...message.author, kind, id: kind === "human" ? "other" : "me" } }} currentUserId="me" actionable onUpdate={vi.fn()}>Original</RoomMessageContent>);
  expect(screen.queryByRole("button", { name: "Message actions" })).not.toBeInTheDocument();
});

it("opens the more menu for a message the viewer did not write when menuExtras exist", async () => {
  render(<RoomMessageContent message={{ ...message, author: { kind: "agent", id: "friday", displayName: "Friday" } }}
    currentUserId="me" actionable onUpdate={vi.fn()}
    menuExtras={<DropdownMenuItem onSelect={vi.fn()}>Pin</DropdownMenuItem>}><p>body</p></RoomMessageContent>);
  await userEvent.click(screen.getByRole("button", { name: "Message actions" }));
  expect(await screen.findByRole("menuitem", { name: "Pin" })).toBeInTheDocument();
  expect(screen.queryByRole("menuitem", { name: "Edit message" })).not.toBeInTheDocument();
});

it("keeps edit and delete above nothing and extras above them on the viewer's own message", async () => {
  render(<RoomMessageContent message={message} currentUserId="me" actionable onUpdate={vi.fn()}
    menuExtras={<DropdownMenuItem onSelect={vi.fn()}>Pin</DropdownMenuItem>}><p>body</p></RoomMessageContent>);
  await userEvent.click(screen.getByRole("button", { name: "Message actions" }));
  expect((await screen.findAllByRole("menuitem")).map((node) => node.textContent)).toEqual([
    "Pin", "Edit message", "Delete message",
  ]);
});

it("is one tab stop: Message actions starts in the tab order, the arrows wrap, Home and End jump, focus moves the stop", async () => {
  const user = userEvent.setup();
  render(<><button type="button">Before</button>
    <RoomMessageContent message={message} currentUserId="me" actionable onUpdate={vi.fn()}
      actions={<><button type="button">React</button><button type="button">Reply</button></>}>Original</RoomMessageContent></>);
  const bar = screen.getByRole("toolbar", { name: "Message tools" });
  const [react, reply, more] = within(bar).getAllByRole("button");
  const stops = () => within(bar).getAllByRole("button").filter((b) => b.tabIndex === 0);
  expect(stops()).toEqual([more]);
  screen.getByRole("button", { name: "Before" }).focus();
  await user.tab();
  expect(more).toHaveFocus();
  await user.keyboard("{ArrowRight}");
  expect(react).toHaveFocus();
  await user.keyboard("{ArrowLeft}");
  expect(more).toHaveFocus();
  await user.keyboard("{Home}");
  expect(react).toHaveFocus();
  await user.keyboard("{End}");
  expect(more).toHaveFocus();
  await user.keyboard("{ArrowLeft}");
  expect(reply).toHaveFocus();
  expect(stops()).toEqual([reply]);
  // Tab leaves the toolbar after one stop, rather than walking every button.
  await user.tab();
  expect(bar).not.toContainElement(document.activeElement as HTMLElement);
});
