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

  it("retries on 504 until the line arrives", async () => {
    vi.useFakeTimers();
    api.post.mockReset()
      .mockRejectedValueOnce(new ApiError(504, null))
      .mockRejectedValueOnce(new ApiError(502, { code: "RUNTIME_UNAVAILABLE" }))
      .mockResolvedValueOnce({ line: "lightbulb join abc", expiresAt: 1 });
    mount();
    fireEvent.click(screen.getByRole("button", { name: "Get my line" }));
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(screen.getByText("Starting your workspace… this can take a minute.")).toBeInTheDocument();
    await act(async () => { await vi.advanceTimersByTimeAsync(20_000); });
    expect(screen.getByText("lightbulb join abc")).toBeInTheDocument();
    expect(api.post).toHaveBeenCalledTimes(3);
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
});
