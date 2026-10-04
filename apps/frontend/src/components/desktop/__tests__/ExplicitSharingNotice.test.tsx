// @vitest-environment jsdom
import React from "react";
import { render, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";

const toast = vi.hoisted(() => vi.fn());
vi.mock("sonner", () => ({ toast }));
import { EXPLICIT_SHARING_SEEN_KEY, ExplicitSharingNotice } from "../ExplicitSharingNotice";

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
  expect(container.textContent).toBe("Sessions are now shared only when you start them in Lightbulb or with lightbulb claude.");
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
