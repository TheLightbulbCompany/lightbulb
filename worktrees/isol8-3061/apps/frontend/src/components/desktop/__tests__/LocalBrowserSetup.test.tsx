// @vitest-environment jsdom
import React from "react";
import { render, screen, waitFor, cleanup } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { LocalBrowserSetup } from "../LocalBrowserSetup";

afterEach(() => { cleanup(); delete (window as Window & { __TAURI__?: unknown }).__TAURI__; });

it("requires explicit local setup and pairing reveal without claiming a connection", async () => {
  const invoke = vi.fn(async (command: string) => command === "local_browser_status"
    ? { extensionEnabled: false } : command === "local_browser_pairing" ? "private-pairing" : undefined);
  Object.assign(window, { __TAURI__: { core: { invoke } } });
  render(<LocalBrowserSetup />);
  await waitFor(() => expect(screen.getByRole("checkbox")).toBeTruthy());
  expect(invoke.mock.calls.map(([command]) => command)).toEqual(["local_browser_status"]);
  await userEvent.click(screen.getByRole("checkbox"));
  await userEvent.click(screen.getByText("Save browser setup"));
  await waitFor(() => expect(invoke).toHaveBeenCalledWith("local_browser_configure", { extension: true }));
  expect(screen.getByRole("status").textContent).toContain("restarting");
  expect(screen.queryByLabelText("Private local pairing code")).toBeNull();
  await userEvent.click(screen.getByText("Reveal local pairing code"));
  expect(await screen.findByLabelText("Private local pairing code")).toHaveProperty("value", "private-pairing");
});
