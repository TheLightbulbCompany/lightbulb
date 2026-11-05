// The "Replying to" chip swaps the composer's clientMessageId (a remount): an attachment must survive that,
// stay out of a request reply, and a focusToken bumped with the swap still focuses the field.
import { useState } from "react";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { MessageAttachment } from "@lightbulb/shared";
import { RoomComposer } from "../RoomComposer";

const attachment: MessageAttachment = { id: "att-1", fileName: "notes.txt", contentType: "text/plain", size: 5 };
const ROOM_ID = "11111111-1111-4111-8111-111111111111";
const CHIP_ID = "22222222-2222-4222-8222-222222222222";

function Harness({ chip, focusToken }: { chip: boolean; focusToken: number }) {
  const [text, setText] = useState("");
  return <RoomComposer text={text} onTextChange={setText} members={[]} sendState="idle" onSubmit={vi.fn()} focusToken={focusToken}
    clientMessageId={chip ? CHIP_ID : ROOM_ID} onUploadAttachment={chip ? undefined : async () => attachment} />;
}

beforeEach(() => vi.stubGlobal("URL", { ...URL, createObjectURL: vi.fn(() => "blob:x"), revokeObjectURL: vi.fn() }));
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("an attachment survives the chip going on and off, hidden while it is on", async () => {
  const view = render(<Harness chip={false} focusToken={0} />);
  fireEvent.drop(screen.getByRole("form", { name: "Message composer" }),
    { dataTransfer: { types: ["Files"], files: [new File(["hello"], "notes.txt", { type: "text/plain" })] } });
  await waitFor(() => expect(screen.getByText("Ready")).toBeInTheDocument());
  view.rerender(<Harness chip focusToken={0} />);
  expect(screen.queryByText("notes.txt")).toBeNull();
  view.rerender(<Harness chip={false} focusToken={1} />);
  expect(screen.getByText("notes.txt")).toBeInTheDocument();
  expect(screen.getByText("Ready")).toBeInTheDocument();
  // The focusToken bump that came with the remount still focuses the field.
  expect(screen.getByRole("textbox", { name: "Message" })).toHaveFocus();
});
