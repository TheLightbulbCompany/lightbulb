import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import type { EngineRecord, SessionRow } from "@lightbulb/shared";
import { NetworkSessionRow } from "../NetworkSessionRow";

const base: SessionRow = { id: "mac/claude:1", source: "network", person: { name: "Dana", isViewer: false }, agent: "claude", title: "Fix the build", folder: "web", state: "idle" };
const show = (changes: Partial<SessionRow>) => render(<ul><NetworkSessionRow row={{ ...base, ...changes }} /></ul>);
const invoke = vi.fn((_cmd: string, _args?: unknown): Promise<unknown> => Promise.resolve());
afterEach(() => { invoke.mockClear(); delete (window as unknown as { __TAURI__?: unknown }).__TAURI__; });

it("shows the title, person, folder and state, with nothing to open", () => {
  const { container } = show({});
  expect(container).toHaveTextContent("Fix the build");
  expect(container).toHaveTextContent("Dana · web");
  expect(container.querySelector(".session-row--gone > div.room-directory-entry.room-directory-entry--inert")).toHaveTextContent("Fix the build");
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

it("offers Share on this Mac's own live Claude session only", () => {
  (window as unknown as { __TAURI__: unknown }).__TAURI__ = { core: { invoke } };
  const me = { person: { name: "Me", isViewer: true }, engine: { own: true } as EngineRecord };
  const { rerender } = show(me);
  fireEvent.click(screen.getByRole("button", { name: /Share/ }));
  expect(screen.getByRole("button", { name: /Share/ })).toHaveTextContent("Share");
  expect(invoke).toHaveBeenCalledWith("share_engine_session", { sessionId: "mac/claude:1" });
  rerender(<ul><NetworkSessionRow row={{ ...base }} /></ul>);
  expect(screen.queryByRole("button")).toBeNull();
  rerender(<ul><NetworkSessionRow row={{ ...base, ...me, state: "ended" }} /></ul>);
  expect(screen.queryByRole("button")).toBeNull();
  rerender(<ul><NetworkSessionRow row={{ ...base, ...me, agent: "codex" }} /></ul>);
  expect(screen.queryByRole("button")).toBeNull();
});

it("offers no Share on the viewer's other Mac", () => {
  show({ person: { name: "Me", isViewer: true }, engine: { own: false } as EngineRecord });
  expect(screen.queryByRole("button")).toBeNull();
});

it("shows a refusal in the row, and says nothing when the person pressed No", async () => {
  (window as unknown as { __TAURI__: unknown }).__TAURI__ = { core: { invoke } };
  const { container } = show({ person: { name: "Me", isViewer: true }, engine: { own: true } as EngineRecord });
  invoke.mockRejectedValueOnce("sharing is off on this Mac");
  fireEvent.click(screen.getByRole("button", { name: /Share/ }));
  expect(await screen.findByText("sharing is off on this Mac")).toBeInTheDocument();
  invoke.mockRejectedValueOnce("Not changed: declined on this Mac.");
  fireEvent.click(screen.getByRole("button", { name: /Share/ }));
  await waitFor(() => expect(screen.queryByText("sharing is off on this Mac")).toBeNull());
  await act(async () => {});
  expect(container).not.toHaveTextContent("declined");
});

it("calls the viewer's own person You", () => {
  const { container } = show({ person: { name: "Me", isViewer: true } });
  expect(container).toHaveTextContent("You · web");
  expect(container).not.toHaveTextContent("Me");
});
