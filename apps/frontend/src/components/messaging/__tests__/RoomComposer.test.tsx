import { useState } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { RoomComposer, type RoomComposerProps } from "../RoomComposer";

// Literal synthetic participants: these fixtures are not runtime qualification.
const targets = [
  { ref: "human-alice", name: "Alice Example", kind: "human" as const },
  { ref: "agent-friday", name: "Friday Example", kind: "agent" as const },
];
function props(overrides: Partial<RoomComposerProps> = {}): RoomComposerProps {
  return { body: "A synthetic message", onBodyChange: vi.fn(), targets,
    selectedTargetRefs: [], onTargetsChange: vi.fn(), clientRequestId: "request-fixture-1",
    sendState: "idle", onSubmit: vi.fn(), ...overrides };
}

describe("controlled room composer", () => {
  it("sends supplied text and explicit refs once, without treating callback resolution as saved", async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    render(<RoomComposer {...props({ onSubmit, selectedTargetRefs: ["human-alice", "agent-friday"] })} />);
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    fireEvent.keyDown(screen.getByRole("textbox", { name: "Message" }), { key: "Enter" });
    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1));
    expect(onSubmit).toHaveBeenCalledWith({ body: "A synthetic message", targetRefs: ["human-alice", "agent-friday"], clientRequestId: "request-fixture-1" });
    expect(screen.queryByText("Message saved")).not.toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Message" })).toHaveValue("A synthetic message");
  });

  it.each([
    ["   ", false], ["a".repeat(16384), true], ["a".repeat(16385), false],
    ["😀".repeat(4096), true], ["😀".repeat(4096) + "a", false],
  ])("checks nonempty text and UTF-8 limit (case %#)", (body, allowed) => {
    const onSubmit = vi.fn();
    render(<RoomComposer {...props({ body, onSubmit })} />);
    fireEvent.keyDown(screen.getByRole("textbox", { name: "Message" }), { key: "Enter" });
    expect(onSubmit).toHaveBeenCalledTimes(allowed ? 1 : 0);
  });

  it("never submits during IME composition or with Shift+Enter", () => {
    const onSubmit = vi.fn();
    render(<RoomComposer {...props({ onSubmit })} />);
    const textbox = screen.getByRole("textbox", { name: "Message" });
    fireEvent.compositionStart(textbox);
    fireEvent.keyDown(textbox, { key: "Enter" });
    fireEvent.compositionEnd(textbox);
    fireEvent.keyDown(textbox, { key: "Enter", shiftKey: true });
    fireEvent.keyDown(textbox, { key: "Enter", keyCode: 229 });
    expect(onSubmit).not.toHaveBeenCalled();
    fireEvent.keyDown(textbox, { key: "Enter" });
    expect(onSubmit).toHaveBeenCalledTimes(1);
  });

  it("separates human notifications from agent work; target choice alone never sends", () => {
    const onSubmit = vi.fn();
    function Fixture() {
      const [selectedTargetRefs, onTargetsChange] = useState<string[]>([]);
      return <RoomComposer {...props({ body: "@Friday Example is only text", onSubmit, selectedTargetRefs, onTargetsChange })} />;
    }
    render(<Fixture />);
    fireEvent.click(screen.getByRole("button", { name: "Choose mentions and agents" }));
    fireEvent.click(screen.getByRole("checkbox", { name: /Friday Example.*Request work/ }));
    expect(onSubmit).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("checkbox", { name: /Alice Example.*Notify/ }));
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    expect(onSubmit).toHaveBeenCalledWith({ body: "@Friday Example is only text", targetRefs: ["agent-friday", "human-alice"], clientRequestId: "request-fixture-1" });
  });

  it("only uses an explicitly supplied agent DM default and rejects unauthorized refs or over ten distinct targets", () => {
    const onSubmit = vi.fn();
    const { rerender } = render(<RoomComposer {...props({ onSubmit, defaultAgentRef: "agent-friday" })} />);
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({ targetRefs: ["agent-friday"] }));
    rerender(<RoomComposer {...props({ onSubmit, clientRequestId: "second", selectedTargetRefs: ["not-authorized"] })} />);
    expect(screen.getByRole("button", { name: "Send message" })).toBeDisabled();
    const many = Array.from({ length: 11 }, (_, i) => ({ ref: `synthetic-${i}`, name: `Person ${i}`, kind: "human" as const }));
    rerender(<RoomComposer {...props({ onSubmit, clientRequestId: "third", targets: many, selectedTargetRefs: many.map(p => p.ref) })} />);
    expect(screen.getByRole("button", { name: "Send message" })).toBeDisabled();
  });

  it("holds uncertain sends for reconciliation and retries rejected sends only deliberately", () => {
    const onSubmit = vi.fn(), onReconcile = vi.fn(), onRetry = vi.fn();
    const { rerender } = render(<RoomComposer {...props({ onSubmit, onReconcile, sendState: "uncertain" })} />);
    fireEvent.keyDown(screen.getByRole("textbox", { name: "Message" }), { key: "Enter" });
    expect(onSubmit).not.toHaveBeenCalled();
    expect(screen.getByRole("textbox", { name: "Message" })).toHaveValue("A synthetic message");
    fireEvent.click(screen.getByRole("button", { name: "Check message status" }));
    expect(onReconcile).toHaveBeenCalledWith("request-fixture-1");
    rerender(<RoomComposer {...props({ onSubmit, onRetry, sendState: "rejected" })} />);
    fireEvent.keyDown(screen.getByRole("textbox", { name: "Message" }), { key: "Enter" });
    expect(onSubmit).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Retry message" }));
    fireEvent.click(screen.getByRole("button", { name: "Retry message" }));
    expect(onRetry).toHaveBeenCalledTimes(1);
    expect(onRetry).toHaveBeenCalledWith(expect.objectContaining({ clientRequestId: "request-fixture-1" }));
  });

  it("does not replay an uncertain attempt if a parent temporarily returns to idle", () => {
    const onSubmit = vi.fn();
    const { rerender } = render(<RoomComposer {...props({ onSubmit, sendState: "uncertain" })} />);
    rerender(<RoomComposer {...props({ onSubmit, sendState: "idle" })} />);
    fireEvent.keyDown(screen.getByRole("textbox", { name: "Message" }), { key: "Enter" });
    expect(onSubmit).not.toHaveBeenCalled();
    expect(screen.getByRole("status")).toHaveTextContent(/uncertain/i);
  });

  it("allows another deliberate retry after a new authoritative rejection, retaining the same identity", () => {
    const onRetry = vi.fn();
    const base = props({ onRetry, sendState: "rejected" });
    const { rerender } = render(<RoomComposer {...base} />);
    fireEvent.click(screen.getByRole("button", { name: "Retry message" }));
    rerender(<RoomComposer {...base} sendState="sending" />);
    rerender(<RoomComposer {...base} sendState="rejected" />);
    fireEvent.click(screen.getByRole("button", { name: "Retry message" }));
    expect(onRetry).toHaveBeenCalledTimes(2);
    expect(onRetry).toHaveBeenLastCalledWith({ body: "A synthetic message", targetRefs: [], clientRequestId: "request-fixture-1" });
  });

  it("does not invoke an agent by parsing message text", () => {
    const onSubmit = vi.fn();
    render(<RoomComposer {...props({ onSubmit, body: "@Friday Example investigate" })} />);
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({ targetRefs: [] }));
  });

  it("holds a callback failure as uncertain until supplied canonical evidence resolves it", async () => {
    const onSubmit = vi.fn().mockRejectedValue(new Error("Synthetic transport failure"));
    const base = props({ onSubmit, onReconcile: vi.fn() });
    const { rerender } = render(<RoomComposer {...base} />);
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent(/uncertain/i));
    expect(screen.getByRole("button", { name: "Send message" })).toBeDisabled();
    rerender(<RoomComposer {...base} sendState="persisted" />);
    expect(screen.getByRole("status")).toHaveTextContent("Message saved");
    expect(screen.queryByRole("button", { name: "Check message status" })).not.toBeInTheDocument();
  });

  it("reconciles an uncertain retry even when the parent has not yet changed rejected state", async () => {
    render(<RoomComposer {...props({ sendState: "rejected", onRetry: vi.fn().mockRejectedValue(new Error("Synthetic lost retry acknowledgment")), onReconcile: vi.fn() })} />);
    fireEvent.click(screen.getByRole("button", { name: "Retry message" }));
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent(/uncertain/i));
    expect(screen.getByRole("button", { name: "Check message status" })).toBeEnabled();
  });

  it("keeps canonical persistence evidence when an older callback later fails", async () => {
    let reject!: (reason: Error) => void;
    const onSubmit = () => new Promise<void>((_resolve, rejectPromise) => { reject = rejectPromise; });
    const base = props({ onSubmit });
    const { rerender } = render(<RoomComposer {...base} />);
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    rerender(<RoomComposer {...base} sendState="persisted" />);
    reject(new Error("Synthetic late failure"));
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("Message saved"));
  });

  it("can remove revoked explicit targets, then send to the remaining authorized people", () => {
    const onSubmit = vi.fn();
    function Fixture() {
      const [selectedTargetRefs, onTargetsChange] = useState(["human-alice", "revoked-fixture"]);
      return <RoomComposer {...props({ selectedTargetRefs, onTargetsChange, onSubmit })} />;
    }
    render(<Fixture />);
    expect(screen.getByRole("button", { name: "Send message" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Remove unavailable recipients" }));
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({ targetRefs: ["human-alice"] }));
  });
});
