import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import React from "react";

import { McpServersTab } from "../McpServersTab";

const mockGet = vi.fn();
// A stable object identity, matching the real useApi()'s useMemo — the
// component's fetchServers useCallback depends on `api`, so a fresh object
// per render (the naive mock) refires the fetch effect every render.
const apiMock = { get: mockGet };
vi.mock("@/lib/api", () => ({
  useApi: () => apiMock,
}));
vi.mock("@/lib/notify", () => ({ notifyApiError: vi.fn() }));

describe("McpServersTab — fetch failures", () => {
  beforeEach(() => {
    mockGet.mockReset();
    vi.spyOn(console, "error").mockImplementation(() => {});
  });

  it("renders the server list on success", async () => {
    mockGet.mockResolvedValue({ servers: {} });
    render(<McpServersTab />);
    await waitFor(() => expect(screen.getByText("MCP Servers")).toBeInTheDocument());
    expect(screen.queryByText("Failed to load MCP servers.")).toBeNull();
  });

  // H4: a non-403 failure used to be console.error-only, leaving the panel
  // looking like an empty "no servers configured" list.
  it("surfaces a non-403 fetch failure with a retry, instead of failing silently", async () => {
    mockGet.mockRejectedValueOnce(new Error("network down"));
    render(<McpServersTab />);
    await waitFor(() => expect(screen.getByText("Failed to load MCP servers.")).toBeInTheDocument());

    mockGet.mockResolvedValueOnce({ servers: {} });
    fireEvent.click(screen.getByRole("button", { name: /retry/i }));
    await waitFor(() => expect(mockGet).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.queryByText("Failed to load MCP servers.")).toBeNull());
  });

  it("still shows the admin-only card (not the generic error) on a 403", async () => {
    mockGet.mockRejectedValueOnce({ status: 403 });
    render(<McpServersTab />);
    await waitFor(() => expect(screen.getByText("Org admins only.")).toBeInTheDocument());
    expect(screen.queryByText("Failed to load MCP servers.")).toBeNull();
  });
});
