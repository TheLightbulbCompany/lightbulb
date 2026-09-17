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

  it("renders a sent PDF as an accessible authenticated file card", async () => {
    const user = userEvent.setup();
    const pdf: MessageAttachment = { id: "pdf-1", fileName: "brief.pdf", contentType: "application/pdf", size: 5000 };
    const authenticatedDownload = vi.fn(async () => new Blob(["%PDF-1.7"], { type: "application/pdf" }));
    render(<RoomImageAttachments roomId="room-1" attachments={[pdf]} download={authenticatedDownload} />);

    const card = await screen.findByRole("button", { name: "Open brief.pdf" });
    expect(authenticatedDownload).toHaveBeenCalledWith("room-1", "pdf-1");
    expect(screen.getByText("PDF · 4.9 KB")).toBeInTheDocument();
    await user.click(card);

    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("brief.pdf")).toBeInTheDocument();
  });

  it("renders ZIP as an explicit opaque download without prefetching or previewing it", async () => {
    const user = userEvent.setup();
    const zip: MessageAttachment = { id: "zip-1", fileName: "bundle.zip", contentType: "application/zip", size: 8192 };
    const authenticatedDownload = vi.fn(async () => new Blob(["PK\u0003\u0004"], { type: "application/zip" }));
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    render(<RoomImageAttachments roomId="room-1" attachments={[zip]} download={authenticatedDownload} />);

    const card = screen.getByRole("button", { name: "Download bundle.zip" });
    expect(screen.getByText("ZIP · 8.0 KB")).toBeInTheDocument();
    expect(authenticatedDownload).not.toHaveBeenCalled();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();

    await user.click(card);

    expect(authenticatedDownload).toHaveBeenCalledWith("room-1", "zip-1");
    expect(click).toHaveBeenCalledOnce();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    click.mockRestore();
  });

  it("offers an actionable retry when an authenticated file download fails", async () => {
    const user = userEvent.setup();
    const pdf: MessageAttachment = { id: "pdf-1", fileName: "brief.pdf", contentType: "application/pdf", size: 5000 };
    const authenticatedDownload = vi.fn()
      .mockRejectedValueOnce(new Error("offline"))
      .mockResolvedValueOnce(new Blob(["%PDF-1.7"], { type: "application/pdf" }));
    render(<RoomImageAttachments roomId="room-1" attachments={[pdf]} download={authenticatedDownload} />);

    await user.click(await screen.findByRole("button", { name: "Retry brief.pdf" }));

    expect(await screen.findByRole("button", { name: "Open brief.pdf" })).toBeEnabled();
    expect(authenticatedDownload).toHaveBeenCalledTimes(2);
  });
});
