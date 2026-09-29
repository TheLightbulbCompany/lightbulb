// @vitest-environment jsdom
import React from "react";
import { render, screen, cleanup, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { LocalBrowserSetup } from "../LocalBrowserSetup";

afterEach(() => { cleanup(); delete (window as Window & { __TAURI__?: unknown }).__TAURI__; });

function mount(invoke: (command: string) => Promise<unknown>) {
  Object.assign(window, { __TAURI__: { core: { invoke } } });
  return render(<LocalBrowserSetup />);
}

it("shows ready", async () => {
  mount(vi.fn(async () => ({ state: "ready" })));
  expect((await screen.findByRole("status")).textContent).toBe("Agents use a separate 'Lightbulb agent' Chrome window — ready");
});

it("says it starts when needed", async () => {
  mount(vi.fn(async () => ({ state: "stopped" })));
  expect((await screen.findByRole("status")).textContent).toMatch(/starts when an agent needs it/);
});

it("asks to install Chrome", async () => {
  mount(vi.fn(async () => ({ state: "chrome_missing" })));
  expect((await screen.findByRole("status")).textContent).toBe("Install Google Chrome to let agents browse on this Mac");
});

it("renders nothing when Browser is off", async () => {
  const invoke = vi.fn(async () => ({ state: "browser_off" }));
  const view = mount(invoke);
  await waitFor(() => expect(invoke).toHaveBeenCalled());
  expect(view.container.textContent).toBe("");
});

it("renders nothing on an older app", async () => {
  const invoke = vi.fn(async () => { throw "Command agent_chrome_status not found"; });
  const view = mount(invoke);
  await waitFor(() => expect(invoke).toHaveBeenCalled());
  expect(view.container.textContent).toBe("");
});

it("never calls local_browser_*", async () => {
  const invoke = vi.fn<(command: string) => Promise<unknown>>(async () => ({ state: "ready" }));
  mount(invoke);
  await screen.findByRole("status");
  expect(invoke.mock.calls.map(([command]) => command)).toEqual(["agent_chrome_status"]);
});
