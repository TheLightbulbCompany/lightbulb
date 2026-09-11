import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { requestFocusComposer, consumeFocusComposerRequest, _resetFocusComposerRequestForTests } from "@/lib/focusComposerRequest";
import { RoomComposer, type RoomComposerProps } from "../RoomComposer";

const baseProps: RoomComposerProps = {
  text: "", onTextChange: vi.fn(), members: [], selectedMentions: [],
  onMentionsChange: vi.fn(), clientMessageId: "11111111-1111-4111-8111-111111111111",
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
