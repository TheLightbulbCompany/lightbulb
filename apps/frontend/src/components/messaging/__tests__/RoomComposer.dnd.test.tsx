import { useRef, useState } from "react";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { MessageAttachment } from "@lightbulb/shared";
import { RoomComposer } from "../RoomComposer";

const attachment: MessageAttachment = { id: "att-1", fileName: "shot.png", contentType: "image/png", size: 8 };

function Harness({ onUploadAttachment }: { onUploadAttachment: (file: File) => Promise<MessageAttachment> }) {
  const [text, setText] = useState("");
  return <RoomComposer text={text} onTextChange={setText} members={[]}
    clientMessageId="11111111-1111-4111-8111-111111111111" sendState="idle"
    onSubmit={vi.fn()} onUploadAttachment={onUploadAttachment} />;
}

// A stand-in for the real external drop zone (RoomPanel passes its message
// history container as dropTargetRef) — a sibling node outside the <form>.
function ExternalHarness({ onUploadAttachment }: { onUploadAttachment: (file: File) => Promise<MessageAttachment> }) {
  const [text, setText] = useState("");
  const historyRef = useRef<HTMLDivElement>(null);
  return <>
    <div data-testid="history" ref={historyRef}><div data-testid="history-child">messages</div></div>
    <RoomComposer text={text} onTextChange={setText} members={[]}
      clientMessageId="11111111-1111-4111-8111-111111111111" sendState="idle"
      onSubmit={vi.fn()} onUploadAttachment={onUploadAttachment} dropTargetRef={historyRef} />
  </>;
}

beforeEach(() => vi.stubGlobal("URL", { ...URL, createObjectURL: vi.fn(() => "blob:proof"), revokeObjectURL: vi.fn() }));
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("marks the composer while an image drag is over it and uploads on drop", async () => {
  const upload = vi.fn(async () => attachment);
  render(<Harness onUploadAttachment={upload} />);
  const form = screen.getByRole("form", { name: "Message composer" });
  const file = new File(["x"], "shot.png", { type: "image/png" });
  fireEvent.dragEnter(form, { dataTransfer: { types: ["Files"] } });
  expect(form).toHaveAttribute("data-dropping", "true");
  fireEvent.drop(form, { dataTransfer: { types: ["Files"], files: [file] } });
  await waitFor(() => expect(upload).toHaveBeenCalledWith(file));
  expect(form).not.toHaveAttribute("data-dropping");
});

it("ignores a drag that carries no files", () => {
  render(<Harness onUploadAttachment={vi.fn(async () => attachment)} />);
  const form = screen.getByRole("form", { name: "Message composer" });
  fireEvent.dragEnter(form, { dataTransfer: { types: ["text/plain"] } });
  expect(form).not.toHaveAttribute("data-dropping");
});

it("does not flicker when dragging into child elements", () => {
  render(<Harness onUploadAttachment={vi.fn(async () => attachment)} />);
  const form = screen.getByRole("form", { name: "Message composer" });
  const textarea = form.querySelector("textarea");
  if (!textarea) throw new Error("textarea not found");

  fireEvent.dragEnter(form, { dataTransfer: { types: ["Files"] } });
  expect(form).toHaveAttribute("data-dropping", "true");

  fireEvent.dragEnter(textarea, { dataTransfer: { types: ["Files"] } });
  expect(form).toHaveAttribute("data-dropping", "true");

  fireEvent.dragLeave(textarea, { dataTransfer: { types: ["Files"] } });
  expect(form).toHaveAttribute("data-dropping", "true");

  fireEvent.dragLeave(form, { dataTransfer: { types: ["Files"] } });
  expect(form).not.toHaveAttribute("data-dropping");
});

it("shows hint for non-image files but does not upload them", async () => {
  const upload = vi.fn(async () => attachment);
  render(<Harness onUploadAttachment={upload} />);
  const form = screen.getByRole("form", { name: "Message composer" });
  const pdf = new File(["pdf"], "doc.pdf", { type: "application/pdf" });

  fireEvent.dragEnter(form, { dataTransfer: { types: ["Files"] } });
  expect(form).toHaveAttribute("data-dropping", "true");
  expect(screen.getByText("Drop images to attach")).toBeInTheDocument();

  fireEvent.drop(form, { dataTransfer: { types: ["Files"], files: [pdf] } });
  expect(form).not.toHaveAttribute("data-dropping");
  expect(upload).not.toHaveBeenCalled();
});

it("external drop target handler uses current onUploadAttachment after re-render", async () => {
  const upload1 = vi.fn(async () => attachment);
  const upload2 = vi.fn(async () => ({ ...attachment, id: "att-2" }));
  const { rerender } = render(<ExternalHarness onUploadAttachment={upload1} />);
  const node = screen.getByTestId("history");
  const form = screen.getByRole("form", { name: "Message composer" });
  const file = new File(["x"], "shot.png", { type: "image/png" });

  fireEvent.dragEnter(node, { dataTransfer: { types: ["Files"] } });
  expect(form).toHaveAttribute("data-dropping", "true");
  fireEvent.drop(node, { dataTransfer: { types: ["Files"], files: [file] } });
  await waitFor(() => expect(upload1).toHaveBeenCalledWith(file));

  rerender(<ExternalHarness onUploadAttachment={upload2} />);
  fireEvent.dragEnter(node, { dataTransfer: { types: ["Files"] } });
  fireEvent.drop(node, { dataTransfer: { types: ["Files"], files: [file] } });
  await waitFor(() => expect(upload2).toHaveBeenCalledWith(file));
});

it("keeps the external drop hint through child drag events and only clears on a true node-leave", () => {
  render(<ExternalHarness onUploadAttachment={vi.fn(async () => attachment)} />);
  const node = screen.getByTestId("history");
  const child = screen.getByTestId("history-child");
  const form = screen.getByRole("form", { name: "Message composer" });

  fireEvent.dragEnter(node, { dataTransfer: { types: ["Files"] } });
  expect(form).toHaveAttribute("data-dropping", "true");

  fireEvent.dragEnter(child, { dataTransfer: { types: ["Files"] } });
  expect(form).toHaveAttribute("data-dropping", "true");

  fireEvent.dragLeave(child, { dataTransfer: { types: ["Files"] } });
  expect(form).toHaveAttribute("data-dropping", "true");

  fireEvent.dragLeave(node, { dataTransfer: { types: ["Files"] } });
  expect(form).not.toHaveAttribute("data-dropping");
});

it("clears the external drop hint on a window dragend", () => {
  render(<ExternalHarness onUploadAttachment={vi.fn(async () => attachment)} />);
  const node = screen.getByTestId("history");
  const form = screen.getByRole("form", { name: "Message composer" });

  fireEvent.dragEnter(node, { dataTransfer: { types: ["Files"] } });
  expect(form).toHaveAttribute("data-dropping", "true");

  fireEvent.dragEnd(window);
  expect(form).not.toHaveAttribute("data-dropping");
});
