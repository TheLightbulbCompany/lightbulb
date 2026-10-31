// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import React from "react";

import { NetworkConnect } from "../NetworkConnect";

type W = Window & { __TAURI__?: unknown };
const install = (invoke: (c: string, a?: unknown) => Promise<unknown>) => { (window as W).__TAURI__ = { core: { invoke } }; };
const status = (state: string, extra = {}) => ({ state, workspace: "Acme", notice: null, ...extra });

beforeEach(() => { delete (window as W).__TAURI__; });
afterEach(() => { cleanup(); vi.useRealTimers(); });

describe("NetworkConnect", () => {
  it("renders nothing in a browser", () => {
    const { container } = render(<NetworkConnect />);
    expect(container.firstChild).toBeNull();
  });

  it("renders nothing, and stops polling, on an older app", async () => {
    vi.useFakeTimers();
    const invoke = vi.fn().mockRejectedValue("command network_status not found");
    install(invoke);
    const { container } = render(<NetworkConnect />);
    await act(async () => { await vi.advanceTimersByTimeAsync(130_000); });
    expect(container.firstChild).toBeNull();
    expect(invoke).toHaveBeenCalledTimes(1);
  });

  it.each(["hidden", "off", "connected"])("renders nothing for %s", async (state) => {
    const invoke = vi.fn().mockResolvedValue(status(state));
    install(invoke);
    const { container } = render(<NetworkConnect />);
    await waitFor(() => expect(invoke).toHaveBeenCalled());
    expect(container.firstChild).toBeNull();
  });

  it("shows Connecting, as a status", async () => {
    install(vi.fn().mockResolvedValue(status("connecting")));
    render(<NetworkConnect />);
    expect(await screen.findByText("Connecting your computer to Acme…")).toBeTruthy();
    expect(screen.getByRole("status").textContent).toContain("Connecting your computer to Acme…");
  });

  it.each(["optedOut", "failed"])("%s with Connect used up says to reopen, with no button", async (state) => {
    install(vi.fn().mockResolvedValue(status(state, { connectLimited: true })));
    render(<NetworkConnect />);
    expect(await screen.findByText("Reopen Lightbulb to try again")).toBeTruthy();
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("notice with Leave used up says to reopen, and Got it stays", async () => {
    const invoke = vi.fn((c: string) => Promise.resolve(c === "network_status" ? status("connected", { notice: "Acme", leaveLimited: true }) : undefined));
    install(invoke);
    render(<NetworkConnect />);
    expect(await screen.findByText("Reopen Lightbulb to try again")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Leave" })).toBeNull();
    expect(screen.getByRole("status").textContent).toContain("Your computer joined Acme's network.");
    await userEvent.click(screen.getByRole("button", { name: "Got it" }));
    expect(invoke).toHaveBeenCalledWith("network_notice_seen");
  });

  it.each([{}, { notice: "Acme" }])("agent shell: one calm line and no button (%j)", async (extra) => {
    install(vi.fn().mockResolvedValue(status("agentShell", extra)));
    render(<NetworkConnect />);
    expect(await screen.findByText("Lightbulb was opened from an agent's shell. Reopen it from the Dock to connect.")).toBeTruthy();
    expect(screen.queryByRole("button")).toBeNull();
  });

  it.each(["optedOut", "failed"])("%s offers Connect with no args, and Cancel is calm", async (state) => {
    const invoke = vi.fn((c: string) => c === "network_status" ? Promise.resolve(status(state)) : Promise.reject("Not changed: declined on this Mac."));
    install(invoke);
    render(<NetworkConnect />);
    await userEvent.click(await screen.findByRole("button", { name: "Connect" }));
    expect(invoke).toHaveBeenCalledWith("network_connect");
    await waitFor(() => expect(invoke.mock.calls.filter((c) => c[0] === "network_status").length).toBe(2));
    expect(screen.queryByText(/declined/)).toBeNull();
    expect(screen.queryByText(/error/i)).toBeNull();
  });

  it("notice: Leave and Got it call their commands", async () => {
    const invoke = vi.fn((c: string) => Promise.resolve(c === "network_status" ? status("connected", { notice: "Acme" }) : undefined));
    install(invoke);
    render(<NetworkConnect />);
    expect(await screen.findByText(/Your computer joined Acme's network\. Teammates on it see the names, folders and status of your sessions, never your files or conversations\. Hide a folder:/)).toBeTruthy();
    expect(screen.getByText("lightbulb folder <path> off").tagName).toBe("CODE");
    await userEvent.click(screen.getByRole("button", { name: "Leave" }));
    await userEvent.click(screen.getByRole("button", { name: "Got it" }));
    expect(invoke).toHaveBeenCalledWith("network_leave");
    expect(invoke).toHaveBeenCalledWith("network_notice_seen");
    for (const c of invoke.mock.calls) expect(JSON.stringify(c)).not.toMatch(/ticket/i);
  });
});
