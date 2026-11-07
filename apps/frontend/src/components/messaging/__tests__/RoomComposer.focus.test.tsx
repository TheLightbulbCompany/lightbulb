import { useState } from "react";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ApiError, MESSAGE_ATTACHMENT_ACCEPT } from "@lightbulb/shared";
import { requestFocusComposer, consumeFocusComposerRequest, _resetFocusComposerRequestForTests } from "@/lib/focusComposerRequest";
import { INCOMPLETE_FIRST_REPLIES } from "../FirstReplyChips";
import { RoomComposer, type RoomComposerProps } from "../RoomComposer";
import { caretOffset, composerBox, composerValue } from "@/test-utils/composer";

const baseProps: RoomComposerProps = {
  text: "", onTextChange: vi.fn(), members: [], clientMessageId: "11111111-1111-4111-8111-111111111111",
  sendState: "idle", onSubmit: vi.fn(),
};
beforeEach(() => {
  _resetFocusComposerRequestForTests();
  vi.stubGlobal("URL", { ...URL, createObjectURL: vi.fn(() => "blob:proof"), revokeObjectURL: vi.fn() });
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("consumes an agent focus request and seeds an empty draft", () => {
  const onTextChange = vi.fn();
  requestFocusComposer("agent-friday", "File the inbox");
  render(<RoomComposer {...baseProps} focusAgentId="agent-friday" onTextChange={onTextChange} />);
  expect(onTextChange).toHaveBeenCalledWith("File the inbox");
  expect(screen.getByRole("textbox", { name: "Message" })).toHaveFocus();
});

it("W12-13: never overwrites typed text, and never loses to it: the draft goes under it", () => {
  const onTextChange = vi.fn();
  requestFocusComposer("agent-friday", "Write a doc: …");
  render(<RoomComposer {...baseProps} text="@growth " focusAgentId="agent-friday" onTextChange={onTextChange} />);
  expect(onTextChange).toHaveBeenCalledWith("@growth\n\nWrite a doc: …");
  expect(screen.getByRole("textbox", { name: "Message" })).toHaveFocus();
});

it("a draft already in the composer is not added twice", () => {
  const onTextChange = vi.fn();
  requestFocusComposer("agent-friday", "File the inbox");
  render(<RoomComposer {...baseProps} text="File the inbox" focusAgentId="agent-friday" onTextChange={onTextChange} />);
  expect(onTextChange).not.toHaveBeenCalled();
  expect(screen.getByRole("textbox", { name: "Message" })).toHaveFocus();
});

it("consumes live focus events only for the named agent", () => {
  const onTextChange = vi.fn();
  render(<RoomComposer {...baseProps} focusAgentId="agent-friday" onTextChange={onTextChange} />);
  requestFocusComposer("agent-friday", "File the inbox");
  act(() => window.dispatchEvent(new CustomEvent("getstarted:focus-composer", { detail: { agentId: "agent-atlas" } })));
  expect(onTextChange).not.toHaveBeenCalled();
  expect(screen.getByRole("textbox", { name: "Message" })).not.toHaveFocus();
  act(() => window.dispatchEvent(new CustomEvent("getstarted:focus-composer", { detail: { agentId: "agent-friday" } })));
  expect(onTextChange).toHaveBeenCalledOnce();
  expect(onTextChange).toHaveBeenCalledWith("File the inbox");
  expect(screen.getByRole("textbox", { name: "Message" })).toHaveFocus();
  expect(consumeFocusComposerRequest("agent-friday")).toBeNull();
});

it("keeps the composer focused when Enter sends and clears the draft", () => {
  function Harness() {
    const [text, setText] = useState("Send this");
    const [clientMessageId, setClientMessageId] = useState("11111111-1111-4111-8111-111111111111");
    return <RoomComposer {...baseProps} text={text} onTextChange={setText} clientMessageId={clientMessageId}
      onSubmit={() => { setText(""); setClientMessageId("22222222-2222-4222-8222-222222222222"); }} />;
  }

  render(<Harness />);
  const field = screen.getByRole("textbox", { name: "Message" });
  field.focus();
  fireEvent.keyDown(field, { key: "Enter" });

  expect(field).not.toBeInTheDocument();
  expect(composerValue()).toBe("");
  expect(screen.getByRole("textbox", { name: "Message" })).toHaveFocus();
});

it("uploads and sends an image without text", async () => {
  const onSubmit = vi.fn();
  const onUploadAttachment = vi.fn().mockResolvedValue({
    id: "22222222-2222-4222-8222-222222222222", fileName: "proof.png", contentType: "image/png", size: 8,
  });
  render(<RoomComposer {...baseProps} onSubmit={onSubmit} onUploadAttachment={onUploadAttachment} />);
  const file = new File([new Uint8Array([137, 80, 78, 71, 13, 10, 26, 10])], "proof.png", { type: "image/png" });
  fireEvent.change(document.querySelector('input[type="file"]')!, { target: { files: [file] } });
  await waitFor(() => expect(screen.getByRole("button", { name: "Send message" })).toBeEnabled());
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));
  expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({ text: "", attachmentIds: ["22222222-2222-4222-8222-222222222222"] }));
});

it("labels the picker for files and sends mixed image and PDF attachments", async () => {
  const onSubmit = vi.fn();
  const image = new File(["png"], "proof.png", { type: "image/png" });
  const pdf = new File(["%PDF-1.7"], "brief.pdf", { type: "application/pdf" });
  const onUploadAttachment = vi.fn(async (file: File) => ({
    id: file.name === "proof.png" ? "image-id" : "pdf-id",
    fileName: file.name,
    contentType: file.type as "image/png" | "application/pdf",
    size: file.size,
  }));
  render(<RoomComposer {...baseProps} onSubmit={onSubmit} onUploadAttachment={onUploadAttachment} />);

  expect(screen.getByRole("button", { name: "Attach files" })).toBeEnabled();
  const picker = document.querySelector('input[type="file"]')!;
  expect(picker).toHaveAttribute("accept", MESSAGE_ATTACHMENT_ACCEPT);
  fireEvent.change(picker, { target: { files: [image, pdf] } });

  await waitFor(() => expect(screen.getByRole("button", { name: "Send message" })).toBeEnabled());
  expect(screen.getByText("application/pdf · 8 B")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));
  expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({ attachmentIds: ["image-id", "pdf-id"] }));
});

it("removes a document before send", async () => {
  const pdf = new File(["%PDF"], "brief.pdf", { type: "application/pdf" });
  const upload = vi.fn(async () => ({ id: "pdf-id", fileName: pdf.name, contentType: "application/pdf" as const, size: pdf.size }));
  render(<RoomComposer {...baseProps} text="Keep the text" onUploadAttachment={upload} />);
  fireEvent.change(document.querySelector('input[type="file"]')!, { target: { files: [pdf] } });
  await screen.findByText("Ready");

  fireEvent.click(screen.getByRole("button", { name: "Remove brief.pdf" }));

  expect(screen.queryByText("brief.pdf")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Send message" })).toBeEnabled();
});

it("blocks send when server attachment metadata disagrees with the selected file", async () => {
  const pdf = new File(["%PDF"], "brief.pdf", { type: "application/pdf" });
  render(<RoomComposer {...baseProps} text="Do not lose this" onUploadAttachment={vi.fn(async () => ({
    id: "bad-id", fileName: pdf.name, contentType: "image/png" as const, size: pdf.size,
  }))} />);
  fireEvent.change(document.querySelector('input[type="file"]')!, { target: { files: [pdf] } });

  expect(await screen.findByText("The server returned different file metadata. Remove this file and try again.")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Send message" })).toBeDisabled();
});

it("retries an attachment-only rejected send", async () => {
  const onRetry = vi.fn();
  const onUploadAttachment = vi.fn().mockResolvedValue({
    id: "22222222-2222-4222-8222-222222222222", fileName: "proof.png", contentType: "image/png", size: 3,
  });
  render(<RoomComposer {...baseProps} sendState="rejected" onRetry={onRetry} onUploadAttachment={onUploadAttachment} />);
  fireEvent.change(document.querySelector('input[type="file"]')!, { target: { files: [new File(["png"], "proof.png", { type: "image/png" })] } });
  await waitFor(() => expect(screen.getByRole("button", { name: "Retry message" })).toBeEnabled());
  fireEvent.click(screen.getByRole("button", { name: "Retry message" }));
  expect(onRetry).toHaveBeenCalledWith(expect.objectContaining({ text: "", attachmentIds: ["22222222-2222-4222-8222-222222222222"] }));
});

it("does not silently discard a failed upload when text is present", async () => {
  render(<RoomComposer {...baseProps} text="Keep the image" onUploadAttachment={vi.fn().mockRejectedValue(new Error("Upload failed"))} />);
  fireEvent.change(document.querySelector('input[type="file"]')!, { target: { files: [new File(["png"], "proof.png", { type: "image/png" })] } });
  await screen.findByText("Upload failed");
  expect(screen.getByRole("button", { name: "Send message" })).toBeDisabled();
});

it("replaces an API 400 upload failure with an actionable reason", async () => {
  render(<RoomComposer {...baseProps} onUploadAttachment={vi.fn().mockRejectedValue(new ApiError(400, { code: "INVALID_REQUEST" }, "Upload rejected"))} />);
  const pdf = new File(["%PDF"], "brief.pdf", { type: "application/pdf" });
  fireEvent.change(document.querySelector('input[type="file"]')!, { target: { files: [pdf] } });

  expect(await screen.findByText("The server rejected this file. Check that its contents match its type and the file is within the size limit.")).toBeInTheDocument();
  expect(screen.queryByText("Upload rejected")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Retry brief.pdf" })).toBeEnabled();
});

it("focuses the field with the cursor at the end when focusToken changes", () => {
  const { rerender } = render(<RoomComposer {...baseProps} text="Here's our website: " focusToken={0} />);
  const field = composerBox();
  expect(document.activeElement).not.toBe(field);
  rerender(<RoomComposer {...baseProps} text="Here's our website: " focusToken={1} />);
  expect(document.activeElement).toBe(field);
  expect(caretOffset()).toBe("Here's our website: ".length);
});

it("does not let an incomplete chip prefix be sent, but sends once it is completed", () => {
  const { rerender } = render(<RoomComposer {...baseProps} placeholderDrafts={INCOMPLETE_FIRST_REPLIES} text="Here's our website:" />);
  expect(screen.getByRole("button", { name: "Send message" })).toBeDisabled();
  rerender(<RoomComposer {...baseProps} placeholderDrafts={INCOMPLETE_FIRST_REPLIES} text="I'm starting" />);
  expect(screen.getByRole("button", { name: "Send message" })).toBeDisabled();
  rerender(<RoomComposer {...baseProps} placeholderDrafts={INCOMPLETE_FIRST_REPLIES} text="Here's our website: acme.com" />);
  expect(screen.getByRole("button", { name: "Send message" })).toBeEnabled();
});

it("sends \"I'm starting\" when no placeholder drafts are set", () => {
  render(<RoomComposer {...baseProps} text="I'm starting" />);
  expect(screen.getByRole("button", { name: "Send message" })).toBeEnabled();
});
