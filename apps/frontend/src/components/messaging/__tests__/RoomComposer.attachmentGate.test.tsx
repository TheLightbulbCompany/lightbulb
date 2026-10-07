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
import { MESSAGE_ATTACHMENT_ACCEPT, MESSAGE_ATTACHMENT_CONTENT_TYPES } from "@lightbulb/shared";
import { RoomComposer } from "../RoomComposer";

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
  for (const type of MESSAGE_ATTACHMENT_CONTENT_TYPES) {
    expect(MESSAGE_ATTACHMENT_ACCEPT.split(",")).toContain(type);
  }
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

it.each([
  ["Forecast.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"],
  ["Forecast.XLSX", "application/octet-stream"],
  ["Forecast.xlsx", ""],
])("uploads workbook %s declared as %s", async (name, type) => {
  const canonical = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" as const;
  const upload = vi.fn(async () => ({ ...attachment, fileName: name, contentType: canonical }));
  render(<Harness onUploadAttachment={upload} />);
  drop(new File(["PK\u0003\u0004"], name, { type }));
  await waitFor(() => expect(upload).toHaveBeenCalled());
});

it("never uploads an .xlsx whose declared type contradicts its name", async () => {
  const upload = vi.fn(async () => attachment);
  render(<Harness onUploadAttachment={upload} />);
  drop(new File(["PK\u0003\u0004"], "Forecast.xlsx", { type: "application/pdf" }));
  await screen.findByText(/Unsupported file type/);
  expect(upload).not.toHaveBeenCalled();
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
