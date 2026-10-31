import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import type { SessionRow } from "@lightbulb/shared";
import { NetworkSessionRow } from "../NetworkSessionRow";

const base: SessionRow = { id: "mac/claude:1", source: "network", person: { name: "Dana", isViewer: false }, agent: "claude", title: "Fix the build", folder: "web", state: "idle" };
const show = (changes: Partial<SessionRow>) => render(<ul><NetworkSessionRow row={{ ...base, ...changes }} /></ul>);
const invoke = vi.fn(() => Promise.resolve());
afterEach(() => { invoke.mockClear(); delete (window as unknown as { __TAURI__?: unknown }).__TAURI__; });

it("shows the title, person, folder and state, with nothing to open", () => {
  const { container } = show({});
  expect(container).toHaveTextContent("Fix the build");
  expect(container).toHaveTextContent("Dana · web");
  expect(container.querySelector(".session-row--gone")).not.toBeNull();
  expect(screen.queryByRole("button", { name: /open|join|resume|end/i })).toBeNull();
});

it("says on the network without a folder, and the state in words", () => {
  const { container, rerender } = show({ folder: undefined, state: "offline" });
  expect(container).toHaveTextContent("on the network");
  expect(container).toHaveTextContent("offline");
  for (const [state, words] of [["parked", "parked, wakes when messaged"], ["ended", "ended"], ["waiting", "waiting for you"]] as const) {
    rerender(<ul><NetworkSessionRow row={{ ...base, state }} /></ul>);
    expect(container).toHaveTextContent(words);
  }
});

it("offers Share on the viewer's own live Claude session only", () => {
  (window as unknown as { __TAURI__: unknown }).__TAURI__ = { core: { invoke } };
  const { rerender } = show({ person: { name: "Me", isViewer: true } });
  fireEvent.click(screen.getByRole("button", { name: /Share/ }));
  expect(screen.getByRole("button", { name: /Share/ })).toHaveTextContent("Share");
  expect(invoke).toHaveBeenCalledWith("share_engine_session", { sessionId: "mac/claude:1" });
  rerender(<ul><NetworkSessionRow row={{ ...base }} /></ul>);
  expect(screen.queryByRole("button")).toBeNull();
  rerender(<ul><NetworkSessionRow row={{ ...base, person: { name: "Me", isViewer: true }, state: "ended" }} /></ul>);
  expect(screen.queryByRole("button")).toBeNull();
  rerender(<ul><NetworkSessionRow row={{ ...base, person: { name: "Me", isViewer: true }, agent: "codex" }} /></ul>);
  expect(screen.queryByRole("button")).toBeNull();
});
