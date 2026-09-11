import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { requestFocusComposer, consumeFocusComposerRequest, _resetFocusComposerRequestForTests } from "@/lib/focusComposerRequest";
import { RoomComposer, type RoomComposerProps } from "../RoomComposer";

const baseProps: RoomComposerProps = {
  text: "", onTextChange: vi.fn(), members: [], clientMessageId: "11111111-1111-4111-8111-111111111111",
  sendState: "idle", onSubmit: vi.fn(),
};
beforeEach(() => {
  _resetFocusComposerRequestForTests();
  vi.stubGlobal("URL", { ...URL, createObjectURL: vi.fn(() => "blob:proof"), revokeObjectURL: vi.fn() });
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("describes the message field with the respond hint", () => {
  render(<RoomComposer {...baseProps} roomKind="dm" members={[{
    kind: "agent", id: "agent-friday", displayName: "Friday", mentionHandle: "friday",
    role: "member", listenMode: "mentions", joinedAt: "2026-09-10T00:00:00Z", joinedSeq: 0,
  }]} />);
  const hint = screen.getByText("Friday will reply");

  expect(hint.id).toBeTruthy();
  expect(screen.getByRole("textbox", { name: "Message" }).getAttribute("aria-describedby")).toContain(hint.id);
});

it("consumes an agent focus request and seeds an empty draft", () => {
  const onTextChange = vi.fn();
  requestFocusComposer("agent-friday", "File the inbox");
  render(<RoomComposer {...baseProps} focusAgentId="agent-friday" onTextChange={onTextChange} />);
  expect(onTextChange).toHaveBeenCalledWith("File the inbox");
  expect(screen.getByRole("textbox", { name: "Message" })).toHaveFocus();
});

it("focuses without overwriting typed text", () => {
  const onTextChange = vi.fn();
  requestFocusComposer("agent-friday", "File the inbox");
  render(<RoomComposer {...baseProps} text="Already typing" focusAgentId="agent-friday" onTextChange={onTextChange} />);
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

it("retries an attachment-only rejected send", async () => {
  const onRetry = vi.fn();
  const onUploadAttachment = vi.fn().mockResolvedValue({
    id: "22222222-2222-4222-8222-222222222222", fileName: "proof.png", contentType: "image/png", size: 8,
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
