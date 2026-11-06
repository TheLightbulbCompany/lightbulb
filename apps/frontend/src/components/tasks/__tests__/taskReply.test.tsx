import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { FIXTURES, ROOM, SAM } from "@lightbulb/shared/tasks/fixtures";
import { Fresh, fakeTasksClient } from "./fakeTasks";

const live = vi.hoisted(() => ({ superAgent: true, tasks: null as unknown, messaging: { sendMessage: vi.fn(async () => ({})) } }));
const capture = vi.hoisted(() => vi.fn());
vi.mock("@/hooks/useLiveEvents", () => ({ useLiveEvents: () => live, useOptionalLiveEvents: () => live, useSuperAgent: () => true }));
vi.mock("@/lib/analytics", () => ({ capture }));
vi.mock("@/components/messaging/SessionChat", () => ({ SessionChat: () => <section aria-label="Transcript" /> }));

import { TaskPane } from "../TaskPane";
import { useTaskReply } from "../taskReply";

const step8 = FIXTURES.step08.details["t-signup"];

/** The room's composer as RoomPanel wires it: the chip above, the override's onSubmit, else the room's own send. */
function Composer({ roomId, roomSend, onTextChange }: { roomId: string; roomSend: (text: string) => void; onTextChange: (t: string) => void }) {
  const reply = useTaskReply(roomId, { text: "", onTextChange });
  return <div>
    {reply?.chip}
    <button type="button" onClick={() => void (reply?.composer.onSubmit
      ? reply.composer.onSubmit({ clientMessageId: "c", text: "Make it blue", mentions: [], replyToMessageId: null, threadRootMessageId: null })
      : roomSend("Make it blue"))}>Send</button>
  </div>;
}

beforeEach(() => {
  live.tasks = fakeTasksClient([step8.task], { "t-signup": step8 });
  live.messaging.sendMessage.mockClear();
  capture.mockClear();
});

describe("one text box: the room composer replies to the open thread", () => {
  it("shows Replying to, posts into the task room, and the pane offers no second box", async () => {
    const roomSend = vi.fn();
    const onTextChange = vi.fn();
    render(<Fresh>
      <Composer roomId={ROOM} roomSend={roomSend} onTextChange={onTextChange} />
      <TaskPane taskId="t-signup" viewerId={SAM} onClose={vi.fn()} />
    </Fresh>);
    expect(await screen.findByText(step8.task.title, { selector: ".font-semibold.text-foreground" })).toBeInTheDocument();
    expect(screen.getByText(/Replying to:/)).toBeInTheDocument();
    // The room's composer is on screen: no steer line in the pane.
    expect(screen.queryByRole("textbox", { name: "Steer this request" })).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Send" }));
    expect(live.messaging.sendMessage).toHaveBeenCalledWith(step8.taskRoomId, expect.objectContaining({ text: "Make it blue", mentions: [], threadRootMessageId: null }));
    expect(roomSend).not.toHaveBeenCalled();
    await waitFor(() => expect(onTextChange).toHaveBeenCalledWith(""));
    expect(capture).toHaveBeenCalledWith("task_steer_sent", expect.objectContaining({ taskId: "t-signup", source: "composer" }));
  });

  it("the box and the room's stored draft empty when the steer goes, not when it lands; a failure gives it back", async () => {
    let fail!: (e: Error) => void;
    live.messaging.sendMessage.mockImplementationOnce(() => new Promise((_, reject) => { fail = reject; }));
    const onTextChange = vi.fn();
    render(<Fresh>
      <Composer roomId={ROOM} roomSend={vi.fn()} onTextChange={onTextChange} />
      <TaskPane taskId="t-signup" viewerId={SAM} onClose={vi.fn()} />
    </Fresh>);
    await screen.findByText(/Replying to:/);
    await userEvent.click(screen.getByRole("button", { name: "Send" }));
    // Still in flight: already cleared (no "Drafts › room" while it posts).
    expect(onTextChange).toHaveBeenCalledWith("");
    fail(new Error("offline"));
    await waitFor(() => expect(onTextChange).toHaveBeenLastCalledWith("Make it blue"));
    expect(await screen.findByRole("alert")).toHaveTextContent("didn't go through");
  });

  it("removing the chip sends to the room as normal", async () => {
    const roomSend = vi.fn();
    render(<Fresh>
      <Composer roomId={ROOM} roomSend={roomSend} onTextChange={vi.fn()} />
      <TaskPane taskId="t-signup" viewerId={SAM} onClose={vi.fn()} />
    </Fresh>);
    await userEvent.click(await screen.findByRole("button", { name: `Stop replying to ${step8.task.title}` }));
    expect(screen.queryByText(/Replying to:/)).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Send" }));
    expect(roomSend).toHaveBeenCalledWith("Make it blue");
    expect(live.messaging.sendMessage).not.toHaveBeenCalled();
  });

  it("no chip for another room's thread, or with no pane open", async () => {
    const { unmount } = render(<Fresh><Composer roomId={ROOM} roomSend={vi.fn()} onTextChange={vi.fn()} /></Fresh>);
    expect(screen.queryByText(/Replying to:/)).toBeNull();
    unmount();
    render(<Fresh>
      <Composer roomId="room-elsewhere" roomSend={vi.fn()} onTextChange={vi.fn()} />
      <TaskPane taskId="t-signup" viewerId={SAM} onClose={vi.fn()} />
    </Fresh>);
    // Its own room's composer isn't on screen, so the pane keeps a one-line note.
    expect(await screen.findByRole("textbox", { name: "Steer this request" })).toBeInTheDocument();
    expect(screen.queryByText(/Replying to:/)).toBeNull();
  });
});
