// @vitest-environment jsdom
import React from "react";
import { render, screen, waitFor, cleanup } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { LocalBrowserSetup } from "../LocalBrowserSetup";

afterEach(() => { cleanup(); delete (window as Window & { __TAURI__?: unknown }).__TAURI__; });

function mount(invoke: (command: string) => Promise<unknown>) {
  Object.assign(window, { __TAURI__: { core: { invoke } } });
  render(<LocalBrowserSetup />);
}

it("offers no everyday-Chrome option, and a Mac that had it drops it quietly", async () => {
  const invoke = vi.fn(async (command: string) => {
    if (command === "local_browser_status") return { extensionEnabled: true };
    throw "Command local_browser_configure not found";
  });
  mount(invoke);
  await waitFor(() => expect(invoke).toHaveBeenCalledWith("local_browser_configure", { extension: false }));
  expect(screen.getByText(/Agents use their own Chrome window/)).toBeTruthy();
  expect(screen.queryByRole("checkbox")).toBeNull();
  expect(screen.queryByText(/everyday Chrome|Web Store|pairing code|OpenClaw/)).toBeNull();
  expect(screen.queryByRole("status")).toBeNull();
});

it("leaves a Mac without the everyday profile alone", async () => {
  const invoke = vi.fn(async () => ({ extensionEnabled: false }));
  mount(invoke);
  await waitFor(() => expect(invoke).toHaveBeenCalledWith("local_browser_status"));
  expect(invoke).toHaveBeenCalledTimes(1);
});

it("an old desktop app says to update, never the raw command error", async () => {
  mount(vi.fn(async () => { throw "Command local_browser_status not found"; }));
  expect((await screen.findByRole("status")).textContent).toBe("Update the Lightbulb desktop app, then reopen Settings.");
});
