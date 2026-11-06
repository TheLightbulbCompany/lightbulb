// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RoomImageAttachments } from "../RoomImageAttachments";
import { OutputBackContext } from "@/components/creations/openOutput";
import type { MessageAttachment } from "@lightbulb/shared";

vi.mock("@/components/workspace/workspaceController", () => ({ openPanel: vi.fn() }));

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

  it.each([
    ["memo.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "DOCX"],
    ["deck.pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation", "PPTX"],
  ] as const)("renders %s as an explicit opaque download without prefetching or previewing it", async (fileName, contentType, label) => {
    const user = userEvent.setup();
    const office: MessageAttachment = { id: "office-1", fileName, contentType, size: 8192 };
    const authenticatedDownload = vi.fn(async () => new Blob(["content"], { type: contentType }));
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    render(<RoomImageAttachments roomId="room-1" attachments={[office]} download={authenticatedDownload} />);

    const card = screen.getByRole("button", { name: `Download ${fileName}` });
    expect(screen.getByText(`${label} · 8.0 KB`)).toBeInTheDocument();
    expect(authenticatedDownload).not.toHaveBeenCalled();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();

    await user.click(card);

    expect(authenticatedDownload).toHaveBeenCalledWith("room-1", "office-1");
    expect(click).toHaveBeenCalledOnce();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    click.mockRestore();
  });

  it.each([
    ["report.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "XLSX"],
    ["data.csv", "text/csv", "CSV"],
    ["page.html", "text/html", "HTML"],
    ["notes.md", "text/markdown", "MD"],
    ["report.pdf", "application/pdf", "PDF"],
  ] as const)("opens %s in the artifact pane without prefetching it", async (fileName, contentType, label) => {
    const { openPanel } = await import("@/components/workspace/workspaceController");
    const download = vi.fn();
    render(<RoomImageAttachments roomId="room-1" attachments={[{ id: "a1", fileName, contentType, size: 10 }]} download={download} />);
    const card = screen.getByRole("button", { name: new RegExp(`open ${fileName}`, "i") });
    expect(within(card).getByText(new RegExp(`^${label} ·`))).toBeInTheDocument();
    expect(download).not.toHaveBeenCalled();
    await userEvent.click(card);
    expect(openPanel).toHaveBeenCalledWith("artifact", { src: "attachment", room: "room-1", id: "a1", name: fileName, type: contentType }, { pushUrl: true });
  });

  it("inside an open task, the doc card opens with its way back to the task: '‹ Task' and Escape (W12-5)", async () => {
    const { openPanel } = await import("@/components/workspace/workspaceController");
    render(<OutputBackContext.Provider value="task:t9">
      <RoomImageAttachments roomId="room-1" attachments={[{ id: "a1", fileName: "menu.md", contentType: "text/markdown", size: 328 }]} download={vi.fn()} />
    </OutputBackContext.Provider>);
    await userEvent.click(screen.getByRole("button", { name: "Open menu.md" }));
    expect(openPanel).toHaveBeenLastCalledWith("artifact", expect.objectContaining({ id: "a1", back: "task:t9" }), { pushUrl: true });
  });

  it("plays a video from a presigned URL, never the byte route, with the sibling image as its poster", async () => {
    const downloadSpy = vi.fn(download);
    const attachmentUrl = vi.fn(async () => ({ url: "https://s3.example/hero.mp4?sig=1" }));
    const { container } = render(<RoomImageAttachments roomId="room-1" download={downloadSpy} attachmentUrl={attachmentUrl}
      attachments={[
        { id: "vid-1", fileName: "hero.mp4", contentType: "video/mp4", size: 4_311_540 },
        { id: "poster-1", fileName: "poster.png", contentType: "image/png", size: 2048 },
      ]} />);

    await screen.findByRole("button", { name: "View poster.png" });
    const video = container.querySelector("video");
    expect(video).toHaveAttribute("src", "https://s3.example/hero.mp4?sig=1");
    expect(video).toHaveAttribute("controls");
    expect(video?.getAttribute("poster")).toMatch(/^blob:mock-/);
    expect(screen.getByRole("link", { name: "Download hero.mp4" })).toHaveAttribute("href", "https://s3.example/hero.mp4?sig=1");
    expect(attachmentUrl).toHaveBeenCalledWith("room-1", "vid-1");
    expect(downloadSpy).not.toHaveBeenCalledWith("room-1", "vid-1");
  });

  it("offers a retry that mints a fresh URL when a video fails to load", async () => {
    const user = userEvent.setup();
    let minted = 0;
    const attachmentUrl = vi.fn(async () => ({ url: `https://s3.example/v?n=${++minted}` }));
    const { container } = render(<RoomImageAttachments roomId="room-1" download={download} attachmentUrl={attachmentUrl}
      attachments={[{ id: "vid-1", fileName: "hero.mp4", contentType: "video/mp4", size: 10 }]} />);

    await screen.findByRole("link", { name: "Download hero.mp4" });
    container.querySelector("video")!.dispatchEvent(new Event("error"));
    await user.click(await screen.findByRole("button", { name: /Retry hero.mp4/ }));
    await screen.findByRole("link", { name: "Download hero.mp4" });
    expect(attachmentUrl).toHaveBeenCalledTimes(2);
  });
});
