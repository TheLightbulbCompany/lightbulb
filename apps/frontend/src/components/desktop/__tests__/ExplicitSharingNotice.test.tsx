// @vitest-environment jsdom
import React from "react";
import { render, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";

const toast = vi.hoisted(() => vi.fn());
vi.mock("sonner", () => ({ toast }));
import { EXPLICIT_SHARING_SEEN_KEY, ExplicitSharingNotice, LIVE_SHARING_OFFER_KEY, LiveSharingOffer } from "../ExplicitSharingNotice";

const invoke = vi.fn();
beforeEach(() => {
  toast.mockReset(); invoke.mockReset(); localStorage.clear();
  Object.assign(window, { __TAURI__: { core: { invoke } } });
});

it("tells a Mac that shares sessions, once", async () => {
  invoke.mockResolvedValue({ enrollment: { enabled: true } });
  const first = render(<ExplicitSharingNotice />);
  await waitFor(() => expect(toast).toHaveBeenCalledTimes(1));
  const { container } = render(<>{toast.mock.calls[0][0]}</>);
  expect(container.textContent).toBe("A Claude Code or Codex session on this Mac is shared with your workspace when you start it with Lightbulb, choose Share to Lightbulb in it, or have Share every new session on. Any other session is only yours. Stop sharing one, or turn sharing off in Settings.");
  expect(localStorage.getItem(EXPLICIT_SHARING_SEEN_KEY)).toBe("1");
  first.unmount();
  render(<ExplicitSharingNotice />);
  await Promise.resolve();
  expect(toast).toHaveBeenCalledTimes(1);
});

it("says nothing where sharing is off or outside the desktop app", async () => {
  invoke.mockResolvedValue({ enrollment: null });
  render(<ExplicitSharingNotice />);
  await waitFor(() => expect(invoke).toHaveBeenCalledWith("local_sessions_status"));
  Object.assign(window, { __TAURI__: undefined });
  render(<ExplicitSharingNotice />);
  await Promise.resolve();
  expect(toast).not.toHaveBeenCalled();
  expect(localStorage.getItem(EXPLICIT_SHARING_SEEN_KEY)).toBeNull();
});

const bridge = (sharing: boolean, liveOn: boolean | Error) => invoke.mockImplementation(async (command: string) => {
  if (command === "local_sessions_status") return { enrollment: sharing ? { enabled: true } : null };
  if (command === "shell_integration_status") { if (liveOn instanceof Error) throw liveOn; return liveOn; }
  if (command === "shell_integration_set") return true;
  return null;
});

it("offers Share every new session once to a paired Mac that shares sessions, and turns it on from the offer", async () => {
  bridge(true, false);
  const first = render(<LiveSharingOffer />);
  await waitFor(() => expect(toast).toHaveBeenCalledTimes(1));
  const [title, options] = toast.mock.calls[0];
  expect(title).toBe("Share every new session");
  expect(options.duration).toBe(Infinity);
  expect(options.cancel.label).toBe("Not now");
  expect(localStorage.getItem(LIVE_SHARING_OFFER_KEY)).toBe("1");
  options.action.onClick();
  await waitFor(() => expect(invoke).toHaveBeenCalledWith("shell_integration_set", { enabled: true }));
  await waitFor(() => expect(toast).toHaveBeenCalledTimes(2));
  first.unmount();
  render(<LiveSharingOffer />);
  await Promise.resolve();
  expect(toast).toHaveBeenCalledTimes(2);
});

it("makes no offer where it is already on, where sharing is off, or on an app without the switch", async () => {
  for (const [sharing, liveOn] of [[true, true], [false, false], [true, new Error("unknown command")]] as const) {
    localStorage.clear();
    bridge(sharing, liveOn);
    const view = render(<LiveSharingOffer />);
    await waitFor(() => expect(invoke).toHaveBeenCalledWith("shell_integration_status"));
    await Promise.resolve();
    view.unmount();
    invoke.mockClear();
  }
  expect(toast).not.toHaveBeenCalled();
  expect(localStorage.getItem(LIVE_SHARING_OFFER_KEY)).toBeNull();
});
