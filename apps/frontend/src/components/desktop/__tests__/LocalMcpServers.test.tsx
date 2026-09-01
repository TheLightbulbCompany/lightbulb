// @vitest-environment jsdom
import React from "react";

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { LocalMcpServers } from "../LocalMcpServers";

// PostHog has no place in the test env.
vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));

type InvokeFn = (cmd: string, args?: Record<string, unknown>) => Promise<unknown>;

function installTauriBridge(invoke: InvokeFn) {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  (window as any).__TAURI__ = { core: { invoke } };
}

/**
 * The validation rules have their own suite (lib/__tests__/localMcp.test.ts).
 * This one covers what only the component knows: the browser-vs-desktop fork,
 * the exact IPC command names + argument shapes, and the fact that a write
 * says "the connection restarts" instead of a bare success.
 */
describe("LocalMcpServers", () => {
  beforeEach(() => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    delete (window as any).__TAURI__;
  });

  afterEach(() => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    delete (window as any).__TAURI__;
  });

  it("tells a plain browser to open the desktop app, and calls no IPC", async () => {
    render(<LocalMcpServers />);
    await waitFor(() => {
      expect(screen.getByText(/open it to add or remove them/i)).toBeTruthy();
    });
    expect(screen.queryByLabelText(/Local MCP server configuration/i)).toBeNull();
  });

  // This pane deploys with the web app; `local_mcp_*` ships with the desktop
  // DMG. Because the app loads the remote URL, every already-installed build
  // renders this pane before it has the commands — for however long it takes
  // that user to relaunch. Without this branch they'd see Tauri's raw
  // "not found" string and have no idea it means "update the app".
  it("asks an out-of-date desktop app to update rather than showing the raw IPC error", async () => {
    installTauriBridge(() => Promise.reject("Command local_mcp_list not found"));
    render(<LocalMcpServers />);
    await waitFor(() => {
      expect(screen.getByText(/newer version of the Lightbulb desktop app/i)).toBeTruthy();
    });
    expect(screen.queryByText(/local_mcp_list/)).toBeNull();
  });

  it("lists what local_mcp_list returns", async () => {
    installTauriBridge(
      vi.fn(async () => ({
        filesystem: { command: "npx", args: ["-y", "server-filesystem"] },
      })),
    );

    render(<LocalMcpServers />);

    await waitFor(() => expect(screen.getByText("filesystem")).toBeTruthy());
    expect(screen.getByText(/npx -y server-filesystem/)).toBeTruthy();
  });

  it("sends the WHOLE map to local_mcp_set and warns that the connection restarts", async () => {
    const invoke = vi.fn(async (cmd: string) =>
      cmd === "local_mcp_list" ? { existing: { command: "old" } } : undefined,
    );
    installTauriBridge(invoke);

    render(<LocalMcpServers />);
    await waitFor(() => expect(screen.getByText("existing")).toBeTruthy());

    const textarea = screen.getByLabelText(/Local MCP server configuration/i);
    await userEvent.click(textarea);
    await userEvent.paste('{"added":{"command":"npx"}}');
    await userEvent.click(screen.getByRole("button", { name: /save & restart/i }));

    await waitFor(() =>
      expect(invoke).toHaveBeenCalledWith("local_mcp_set", {
        // A set REPLACES the map, so the pre-existing entry has to ride along.
        servers: { existing: { command: "old" }, added: { command: "npx" } },
      }),
    );
    expect(screen.getByText(/restarts the local agent host/i)).toBeTruthy();
  });

  it("shows a trap error inline and never reaches the IPC", async () => {
    const invoke = vi.fn(async () => ({}));
    installTauriBridge(invoke);

    render(<LocalMcpServers />);
    await waitFor(() => expect(screen.getByText(/No local MCP servers yet/i)).toBeTruthy());

    await userEvent.click(screen.getByLabelText(/Local MCP server configuration/i));
    await userEvent.paste('{"remote":{"command":"npx","disabled":true}}');
    await userEvent.click(screen.getByRole("button", { name: /save & restart/i }));

    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(screen.getByRole("alert").textContent).toContain('"enabled": false');
    expect(invoke).not.toHaveBeenCalledWith("local_mcp_set", expect.anything());
  });

  it("removes by name and warns about the restart", async () => {
    const invoke = vi.fn(async (cmd: string) =>
      cmd === "local_mcp_list" ? { doomed: { command: "npx" } } : true,
    );
    installTauriBridge(invoke);

    render(<LocalMcpServers />);
    await waitFor(() => expect(screen.getByText("doomed")).toBeTruthy());

    await userEvent.click(screen.getByRole("button", { name: /Remove doomed/i }));

    await waitFor(() => expect(invoke).toHaveBeenCalledWith("local_mcp_remove", { name: "doomed" }));
    expect(screen.queryByText("doomed")).toBeNull();
    expect(screen.getByText(/restarts the local agent host/i)).toBeTruthy();
  });
});
