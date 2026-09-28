// @vitest-environment jsdom
import React from "react";
import { render, screen, waitFor, cleanup } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { LocalBrowserSetup } from "../LocalBrowserSetup";

afterEach(() => { cleanup(); delete (window as Window & { __TAURI__?: unknown }).__TAURI__; });

it("the everyday-Chrome opt-in saves on click and reveals pairing only on request", async () => {
  const invoke = vi.fn(async (command: string) => command === "local_browser_status"
    ? { extensionEnabled: false } : command === "local_browser_pairing" ? "private-pairing" : undefined);
  Object.assign(window, { __TAURI__: { core: { invoke } } });
  render(<LocalBrowserSetup />);
  await waitFor(() => expect(screen.getByRole("checkbox")).toBeTruthy());
  expect(invoke.mock.calls.map(([command]) => command)).toEqual(["local_browser_status"]);
  await userEvent.click(screen.getByRole("checkbox"));
  await waitFor(() => expect(invoke).toHaveBeenCalledWith("local_browser_configure", { extension: true }));
  expect((await screen.findByRole("status")).textContent).toBe("Saved.");
  expect(screen.queryByText(/OpenClaw Chrome extension to connect|Chrome MCP|managed independently/)).toBeNull();
  expect(screen.queryByText(/Install the OpenClaw/)).toBeNull();
  const open = vi.spyOn(window, "open").mockReturnValue(null);
  await userEvent.click(screen.getByText("Open in Chrome Web Store"));
  expect(open).toHaveBeenCalledWith("https://chromewebstore.google.com/detail/openclaw/kcdjddhmeafeomebliikmbpblkmkfoig", "_blank");
  expect(screen.queryByLabelText("Private local pairing code")).toBeNull();
  await userEvent.click(screen.getByText("Show pairing code"));
  expect(await screen.findByLabelText("Private local pairing code")).toHaveProperty("value", "private-pairing");
});
