// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";
import React from "react";
import { SWRConfig } from "swr";

let admin = true;
vi.mock("@/hooks/useIsWorkspaceAdmin", () => ({ useIsWorkspaceAdmin: () => admin }));
const api = { get: vi.fn(), post: vi.fn() };
vi.mock("@/lib/api", async () => {
  const { ApiError } = await import("@lightbulb/shared");
  return { ApiError, useApi: () => api };
});

import { ApiError } from "@lightbulb/shared";
import { NetworkSection } from "../NetworkSection";

const mount = () => render(<SWRConfig value={{ provider: () => new Map() }}><NetworkSection /></SWRConfig>);

describe("NetworkSection", () => {
  beforeEach(() => {
    admin = true;
    api.get.mockReset().mockResolvedValue({ connected: false });
    api.post.mockReset().mockResolvedValue({ line: "lightbulb join abc", expiresAt: 1 });
  });
  afterEach(() => vi.useRealTimers());

  it("shows the installer and gets a line once", async () => {
    const first = mount();
    expect(screen.getByText(/install\.sh/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Get my line" }));
    expect(await screen.findByText("lightbulb join abc")).toBeInTheDocument();
    expect(api.post).toHaveBeenCalledWith("/settings/network/invite", {});
    first.unmount();
    mount();
    expect(screen.queryByText("lightbulb join abc")).toBeNull();
    expect(screen.getByRole("button", { name: "Get my line" })).toBeInTheDocument();
  });

  it("retries on 503, 504 and RUNTIME_UNAVAILABLE until the line arrives", async () => {
    vi.useFakeTimers();
    api.post.mockReset()
      // API Gateway's HTTP API answers its 30 s integration timeout with 503 (seen on a cold microVM).
      .mockRejectedValueOnce(new ApiError(503, { message: "Service Unavailable" }))
      .mockRejectedValueOnce(new ApiError(504, null))
      .mockRejectedValueOnce(new ApiError(502, { code: "RUNTIME_UNAVAILABLE" }))
      .mockResolvedValueOnce({ line: "lightbulb join abc", expiresAt: 1 });
    mount();
    fireEvent.click(screen.getByRole("button", { name: "Get my line" }));
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(screen.getByText("Starting your workspace… this can take a minute.")).toBeInTheDocument();
    await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
    expect(screen.getByText("lightbulb join abc")).toBeInTheDocument();
    expect(api.post).toHaveBeenCalledTimes(4);
  });

  it("hides Reconnect workspace from members", () => {
    admin = false;
    mount();
    expect(screen.queryByText("Reconnect workspace")).toBeNull();
  });

  it("never says mail", async () => {
    const { container } = mount();
    fireEvent.click(screen.getByRole("button", { name: "Get my line" }));
    await screen.findByText("lightbulb join abc");
    expect(container.textContent).not.toMatch(/mail/i);
  });

  it("reconnects as admin: confirm, POST, revalidate", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    mount();
    await screen.findByText("Reconnect workspace");
    const before = api.get.mock.calls.length;
    fireEvent.click(screen.getByText("Reconnect workspace"));
    await vi.waitFor(() => expect(api.post).toHaveBeenCalledWith("/settings/network/reconnect", {}));
    await vi.waitFor(() => expect(api.get.mock.calls.length).toBeGreaterThan(before));
    expect(window.confirm).toHaveBeenCalledWith("Every computer will need a new line.");
  });

  it("says the workspace is still starting after 12 tries", async () => {
    vi.useFakeTimers();
    api.post.mockReset().mockRejectedValue(new ApiError(504, null));
    mount();
    fireEvent.click(screen.getByRole("button", { name: "Get my line" }));
    await act(async () => { await vi.advanceTimersByTimeAsync(11 * 10_000); });
    expect(api.post).toHaveBeenCalledTimes(12);
    expect(screen.getByRole("alert")).toHaveTextContent("Your workspace is still starting. Try again in a few minutes.");
  });
});
