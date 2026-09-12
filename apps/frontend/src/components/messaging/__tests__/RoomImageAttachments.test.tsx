// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RoomImageAttachments } from "../RoomImageAttachments";
import type { MessageAttachment } from "@lightbulb/shared";

const ATTACHMENTS: MessageAttachment[] = [
  { id: "att-1", fileName: "cat.png", contentType: "image/png", size: 2048 },
  { id: "att-2", fileName: "dog.jpg", contentType: "image/jpeg", size: 4096 },
];

// jsdom has no object-URL support; return a stable, distinguishable URL per
// attachment so tests can tell which one is showing.
let objectUrlCalls = 0;
beforeEach(() => {
  objectUrlCalls = 0;
  global.URL.createObjectURL = vi.fn(() => `blob:mock-${++objectUrlCalls}`);
  global.URL.revokeObjectURL = vi.fn();
});

function download(_roomId: string, attachmentId: string): Promise<Blob> {
  return Promise.resolve(new Blob([attachmentId], { type: "image/png" }));
}

describe("RoomImageAttachments", () => {
  it("opens the viewer on a downloaded attachment and returns focus to the trigger on Escape", async () => {
    const user = userEvent.setup();
    render(<RoomImageAttachments roomId="room-1" attachments={ATTACHMENTS} download={download} />);

    const trigger = await screen.findByRole("button", { name: "View cat.png" });
    await user.click(trigger);

    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("cat.png")).toBeInTheDocument();
    expect(within(dialog).getByAltText("cat.png")).toBeInTheDocument();

    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
  });

  it("pages between a message's attachments with ArrowRight/ArrowLeft", async () => {
    const user = userEvent.setup();
    render(<RoomImageAttachments roomId="room-1" attachments={ATTACHMENTS} download={download} />);

    await user.click(await screen.findByRole("button", { name: "View cat.png" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("1 of 2", { exact: false })).toBeInTheDocument();

    await user.keyboard("{ArrowRight}");
    expect(within(dialog).getByText("2 of 2", { exact: false })).toBeInTheDocument();
    expect(within(dialog).getByAltText("dog.jpg")).toBeInTheDocument();

    await user.keyboard("{ArrowLeft}");
    expect(within(dialog).getByText("1 of 2", { exact: false })).toBeInTheDocument();
    expect(within(dialog).getByAltText("cat.png")).toBeInTheDocument();
  });

  it("closes on backdrop-equivalent Escape without leaking the dialog into a single-attachment message", async () => {
    const user = userEvent.setup();
    render(
      <RoomImageAttachments
        roomId="room-1"
        attachments={[ATTACHMENTS[0]]}
        download={download}
      />,
    );
    await user.click(await screen.findByRole("button", { name: "View cat.png" }));
    const dialog = await screen.findByRole("dialog");
    // A single attachment gets no paging controls.
    expect(within(dialog).queryByRole("button", { name: /next attachment/i })).not.toBeInTheDocument();
    expect(within(dialog).queryByRole("button", { name: /previous attachment/i })).not.toBeInTheDocument();
  });
});
