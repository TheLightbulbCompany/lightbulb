// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import React from "react";

import { DesktopUpdater } from "../DesktopUpdater";

// Mock analytics so it doesn't try to hit PostHog from the test env.
vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));

type InvokeFn = (cmd: string, args?: Record<string, unknown>) => Promise<unknown>;

function installTauriBridge(invoke: InvokeFn) {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  (window as any).__TAURI__ = { core: { invoke } };
}

describe("DesktopUpdater webview-driven flow", () => {
  beforeEach(() => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    delete (window as any).__TAURI__;
    vi.restoreAllMocks();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    delete (window as any).__TAURI__;
  });

  it("renders the static version label when not running under Tauri", () => {
    render(<DesktopUpdater version="9.9" />);
    expect(screen.getByText(/Lightbulb v9\.9/i)).toBeTruthy();
    expect(screen.queryByRole("button", { name: /check for updates/i })).toBeNull();
  });

  it("shows the installed native version instead of a stale web label", async () => {
    installTauriBridge(vi.fn(async () => ({ target: "darwin-aarch64", current_version: "0.4.606" })));
    render(<DesktopUpdater version="0.1" />);
    expect(await screen.findByText("Lightbulb v0.4.606")).toBeTruthy();
    expect(screen.queryByText("Lightbulb v0.1")).toBeNull();
  });

  it("does not claim a web version when native identity is unavailable", async () => {
    installTauriBridge(vi.fn(async () => { throw new Error("identity unavailable"); }));
    render(<DesktopUpdater version="0.1" />);
    expect(await screen.findByText(/version unavailable/i)).toBeTruthy();
    expect(screen.queryByText("Lightbulb v0.1")).toBeNull();
  });

  it("exposes useful installation failure details without signed download URLs", async () => {
    installTauriBridge(vi.fn(async (cmd: string) => {
      if (cmd === "updater_identity") return { target: "darwin-aarch64", current_version: "0.4.603" };
      throw "install: signature verification failed for https://example.com/app?token=PRIVATE_TOKEN";
    }));
    const manifest = { version: "0.4.606", url: "https://example.com/app", signature: "signature", notes: null, pub_date: null };
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ status: 200, ok: true, json: async () => manifest }));
    render(<DesktopUpdater />);
    await userEvent.click(await screen.findByRole("button", { name: /check for updates/i }));
    await userEvent.click(await screen.findByRole("button", { name: /install v0.4.606/i }));
    expect(await screen.findByText(/install: signature verification failed/)).toBeTruthy();
    expect(document.body.textContent).not.toContain("PRIVATE_TOKEN");
    expect(screen.getByRole("button", { name: /retry/i })).toBeTruthy();
  });

  it("fetches manifest via webview, shows the install button, then hands manifest to Rust", async () => {
    const invoke = vi.fn(async (cmd: string) => {
      if (cmd === "updater_identity") {
        return { target: "darwin-aarch64", current_version: "0.1.0" };
      }
      if (cmd === "apply_update_manifest") {
        // In real life Rust restarts the app — the promise never
        // resolves. In tests we just resolve so the assertion can fire.
        return undefined;
      }
      throw new Error(`unexpected invoke: ${cmd}`);
    });
    installTauriBridge(invoke);

    const manifest = {
      version: "0.2.0",
      notes: "release notes",
      pub_date: "2026-05-15T00:00:00Z",
      url: "https://github.com/TheLightbulbCompany/lightbulb/releases/download/desktop-dev-v0.2.0/Isol8_0.2.0_aarch64.app.tar.gz",
      signature: "untrusted comment: ...",
    };

    const fetchMock = vi.fn().mockResolvedValue({
      status: 200,
      ok: true,
      json: async () => manifest,
    });
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (global as any).fetch = fetchMock;

    render(<DesktopUpdater version="0.1.0" />);

    // The button only renders after the Tauri-detect effect runs.
    const checkButton = await screen.findByRole("button", { name: /check for updates/i });
    await userEvent.click(checkButton);

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledTimes(1);
    });
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/desktop/update/darwin-aarch64/0.1.0");
    expect(init.credentials).toBe("include");

    // Once the manifest comes back the install button replaces the check button.
    const installButton = await screen.findByRole("button", { name: /install v0\.2\.0/i });
    await userEvent.click(installButton);

    await waitFor(() => {
      expect(invoke).toHaveBeenCalledWith("apply_update_manifest", { manifest });
    });
  });

  it("flips to up_to_date on 204", async () => {
    const invoke = vi.fn(async (cmd: string) => {
      if (cmd === "updater_identity") {
        return { target: "darwin-aarch64", current_version: "0.1.0" };
      }
      throw new Error(`unexpected invoke: ${cmd}`);
    });
    installTauriBridge(invoke);

    const fetchMock = vi.fn().mockResolvedValue({
      status: 204,
      ok: true,
      // res.json() would throw; the component must short-circuit on 204.
      json: async () => {
        throw new Error("should not parse 204");
      },
    });
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (global as any).fetch = fetchMock;

    render(<DesktopUpdater version="0.1.0" />);
    const checkButton = await screen.findByRole("button", { name: /check for updates/i });
    await userEvent.click(checkButton);

    expect(await screen.findByText(/up to date/i)).toBeTruthy();
  });

  it("surfaces a friendly error on a manifest-fetch failure", async () => {
    const invoke = vi.fn(async () => ({ target: "darwin-aarch64", current_version: "0.1.0" }));
    installTauriBridge(invoke);

    const fetchMock = vi.fn().mockResolvedValue({
      status: 503,
      ok: false,
      statusText: "Service Unavailable",
      text: async () => "down for maintenance",
    });
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (global as any).fetch = fetchMock;

    render(<DesktopUpdater version="0.1.0" />);
    const checkButton = await screen.findByRole("button", { name: /check for updates/i });
    await userEvent.click(checkButton);

    expect(await screen.findByText(/update error/i)).toBeTruthy();
  });
});
