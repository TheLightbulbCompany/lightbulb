/**
 * Regression guard for the desktop "HTTP 400 on attach" report (2026-09-17).
 *
 * The desktop app is a thin WKWebView over the LIVE web app, so it ran whatever
 * prod had last been promoted. That build's composer uploaded any file whose
 * `type` merely started with `image/`, with no type or size check of its own —
 * so a macOS-native HEIC out of Photos, or an oversized photo, reached
 * `POST /messaging/rooms/{id}/attachments` and came back as a bare 400 from
 * `validate_attachment_bytes`.
 *
 * These cases pin the client-side gate that must stop all of that BEFORE any
 * request leaves the app. The formats below are the ones macOS actually hands a
 * webview from Finder/Photos drags — they are the report, not hypotheticals.
 */
import { useState } from "react";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { MessageAttachment } from "@lightbulb/shared";
import { MESSAGE_ATTACHMENT_ACCEPT, MESSAGE_ATTACHMENT_UPLOAD_CONTENT_TYPES } from "@lightbulb/shared";
import { RoomComposer } from "../RoomComposer";
import { INCOMPLETE_FIRST_REPLIES } from "../FirstReplyChips";

const attachment: MessageAttachment = { id: "att-1", fileName: "shot.png", contentType: "image/png", size: 1 };

function Harness({ onUploadAttachment }: { onUploadAttachment: (file: File) => Promise<MessageAttachment> }) {
  const [text, setText] = useState("");
  return <RoomComposer text={text} onTextChange={setText} members={[]}
    clientMessageId="11111111-1111-4111-8111-111111111111" sendState="idle"
    onSubmit={vi.fn()} onUploadAttachment={onUploadAttachment} />;
}

function drop(file: File) {
  const form = screen.getByRole("form", { name: "Message composer" });
  fireEvent.drop(form, { dataTransfer: { types: ["Files"], files: [file] } });
}

beforeEach(() => vi.stubGlobal("URL", { ...URL, createObjectURL: vi.fn(() => "blob:proof"), revokeObjectURL: vi.fn() }));
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("offers the attach control, so the desktop webview has a visible way in", () => {
  render(<Harness onUploadAttachment={vi.fn(async () => attachment)} />);
  expect(screen.getByRole("button", { name: "Attach files" })).toBeInTheDocument();
});

// The `accept` list drives the macOS open panel. Every type the app is willing
// to upload has to appear there, or the native picker hides files the composer
// would otherwise have taken.
it("advertises every supported type on the file input", () => {
  render(<Harness onUploadAttachment={vi.fn(async () => attachment)} />);
  const input = document.querySelector('input[type="file"]');
  expect(input).toHaveAttribute("accept", MESSAGE_ATTACHMENT_ACCEPT);
  for (const type of MESSAGE_ATTACHMENT_UPLOAD_CONTENT_TYPES) {
    expect(MESSAGE_ATTACHMENT_ACCEPT.split(",")).toContain(type);
  }
  // Video is agent-sent only: a 100 MiB cap cannot ride the upload route.
  expect(MESSAGE_ATTACHMENT_ACCEPT).not.toContain("video/");
});

// HEIC is what Photos and a modern iPhone hand over by default, and it passes a
// naive `image/` prefix test. The backend has no HEIC signature, so every one of
// these used to come back 400.
it.each([
  ["photo.heic", "image/heic"],
  ["scan.tiff", "image/tiff"],
  ["old.bmp", "image/bmp"],
  ["logo.svg", "image/svg+xml"],
])("never uploads %s (%s) — it has no backend signature", async (name, type) => {
  const upload = vi.fn(async () => attachment);
  render(<Harness onUploadAttachment={upload} />);
  drop(new File(["x"], name, { type }));
  await screen.findByText(/Unsupported file type/);
  expect(upload).not.toHaveBeenCalled();
});

it("never uploads an image past the 5 MiB image cap", async () => {
  const upload = vi.fn(async () => attachment);
  render(<Harness onUploadAttachment={upload} />);
  const big = new File(["x"], "huge.png", { type: "image/png" });
  Object.defineProperty(big, "size", { value: 5 * 1024 * 1024 + 1 });
  drop(big);
  await screen.findByText(/Images must be 5 MiB or smaller/);
  expect(upload).not.toHaveBeenCalled();
});

it("never uploads a PDF past the 20 MiB document cap", async () => {
  const upload = vi.fn(async () => attachment);
  render(<Harness onUploadAttachment={upload} />);
  const big = new File(["x"], "deck.pdf", { type: "application/pdf" });
  Object.defineProperty(big, "size", { value: 20 * 1024 * 1024 + 1 });
  drop(big);
  await screen.findByText(/PDFs must be 20 MiB or smaller/);
  expect(upload).not.toHaveBeenCalled();
});

// Text and Office types don't share the image tier -- the validation copy
// must name the right kind and the right cap, not just reject the file.
it("never uploads a CSV past the 5 MiB text cap, and does not call it an image", async () => {
  const upload = vi.fn(async () => attachment);
  render(<Harness onUploadAttachment={upload} />);
  const big = new File(["x"], "big.csv", { type: "text/csv" });
  Object.defineProperty(big, "size", { value: 5 * 1024 * 1024 + 1 });
  drop(big);
  await screen.findByText(/Files must be 5 MiB or smaller/);
  expect(upload).not.toHaveBeenCalled();
});

it("never uploads an XLSX past the 20 MiB document cap, and does not call it an image", async () => {
  const upload = vi.fn(async () => attachment);
  render(<Harness onUploadAttachment={upload} />);
  const big = new File(["x"], "big.xlsx", { type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" });
  Object.defineProperty(big, "size", { value: 20 * 1024 * 1024 + 1 });
  drop(big);
  await screen.findByText(/Files must be 20 MiB or smaller/);
  expect(upload).not.toHaveBeenCalled();
});

// A dropped folder arrives as a File with an empty type and no extension.
it("never uploads a dropped folder", async () => {
  const upload = vi.fn(async () => attachment);
  render(<Harness onUploadAttachment={upload} />);
  drop(new File([], "Screenshots", { type: "" }));
  await screen.findByText(/Unsupported file type/);
  expect(upload).not.toHaveBeenCalled();
});

// Finder's "Compress" output. macOS reports `application/zip`; Windows Chrome
// reports the `x-zip-compressed` alias. Both have to reach the server as the one
// canonical type the backend stores.
it.each([
  ["Archive.zip", "application/zip"],
  ["Archive.zip", "application/x-zip-compressed"],
  ["Archive.ZIP", "application/zip"],
])("uploads %s declared as %s", async (name, type) => {
  const upload = vi.fn(async () => ({ ...attachment, fileName: name, contentType: "application/zip" as const }));
  render(<Harness onUploadAttachment={upload} />);
  drop(new File(["x"], name, { type }));
  await waitFor(() => expect(upload).toHaveBeenCalled());
});

// A `.zip` name with a contradictory declared type is the one case the backend
// raises on rather than guessing — the client must not send it either.
it("never uploads a .zip whose declared type contradicts its name", async () => {
  const upload = vi.fn(async () => attachment);
  render(<Harness onUploadAttachment={upload} />);
  drop(new File(["x"], "Archive.zip", { type: "image/png" }));
  await screen.findByText(/Unsupported file type/);
  expect(upload).not.toHaveBeenCalled();
});

// In the starter channel a tapped chip is a prefix, not a message. An attachment
// alone may be sent, but it must not make the bare prefix sendable: the one-shot
// onboarding capture reads the text and would be spent on "Here's our website:".
it("keeps an incomplete chip prefix unsendable even with an attachment", async () => {
  const props = { onTextChange: vi.fn(), members: [], clientMessageId: "11111111-1111-4111-8111-111111111111",
    sendState: "idle" as const, onSubmit: vi.fn(), onUploadAttachment: vi.fn(async () => attachment), placeholderDrafts: INCOMPLETE_FIRST_REPLIES };
  const { rerender } = render(<RoomComposer {...props} text="" />);
  drop(new File(["x"], "shot.png", { type: "image/png" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "Send message" })).toBeEnabled());
  rerender(<RoomComposer {...props} text="Here's our website:" />);
  expect(screen.getByRole("button", { name: "Send message" })).toBeDisabled();
  rerender(<RoomComposer {...props} text="Here's our website: acme.com" />);
  expect(screen.getByRole("button", { name: "Send message" })).toBeEnabled();
});
