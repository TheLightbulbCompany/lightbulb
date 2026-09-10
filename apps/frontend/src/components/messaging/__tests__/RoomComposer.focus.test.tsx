import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { requestFocusComposer, consumeFocusComposerRequest, _resetFocusComposerRequestForTests } from "@/lib/focusComposerRequest";
import { RoomComposer, type RoomComposerProps } from "../RoomComposer";

const baseProps: RoomComposerProps = {
  text: "", onTextChange: vi.fn(), members: [], selectedMentions: [],
  onMentionsChange: vi.fn(), clientMessageId: "11111111-1111-4111-8111-111111111111",
  sendState: "idle", onSubmit: vi.fn(),
};
beforeEach(_resetFocusComposerRequestForTests);
afterEach(cleanup);

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
