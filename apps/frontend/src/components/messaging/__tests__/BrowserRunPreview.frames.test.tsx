// @vitest-environment jsdom
import { describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";

const api = vi.hoisted(() => ({ get: () => Promise.resolve({ active: true, url: "https://signed" }) }));
vi.mock("@/lib/api", () => ({ useApi: () => api }));
const controller = vi.hoisted(() => ({ showBrowserBesideChat: vi.fn(), showMacRunInBrowser: vi.fn(() => true) }));
vi.mock("@/components/workspace/workspaceController", () => controller);
vi.mock("@/components/workspace/panels/sharedLiveView", () => ({
  useSharedLiveView: () => ({ status: "elsewhere" }),
  useLiveViewWatch: () => undefined,
  LiveViewMirror: () => <div data-testid="browser-mirror" />,
}));
vi.mock("@/components/workspace/panels/BrowserFrameView", () => ({
  BrowserFrameView: ({ runId, interactive, heartbeat, heartbeatBody, mac, prefer }: {
    runId?: string; interactive?: boolean; heartbeat?: string; heartbeatBody?: object; mac?: { deviceId: string }; prefer?: string;
  }) =>
    <div data-testid="frames" data-run={runId} data-interactive={String(!!interactive)} data-heartbeat={heartbeat}
      data-body={JSON.stringify(heartbeatBody)} data-mac={mac?.deviceId} data-prefer={prefer} />,
}));

import { BrowserRunPreview } from "../BrowserRunPreview";

it("shows the run's own tab as live frames when another screen holds the live view", async () => {
  render(<BrowserRunPreview agentName="Friday" site={null} runId="run-1" />);
  await act(async () => { await Promise.resolve(); await Promise.resolve(); });
  expect(screen.getByTestId("frames")).toHaveAttribute("data-run", "run-1");
  // A picture: a click on the card opens the panel, where the person sees what they click.
  expect(screen.getByTestId("frames")).toHaveAttribute("data-interactive", "false");
  expect(screen.queryByTestId("browser-mirror")).toBeNull();
});

it("a run driving a Mac tab shows that tab's thumbnail, view-only, beating the thumb tier", async () => {
  const { MacBrowserPreview, usesMacBrowser } = await import("../BrowserRunPreview");
  expect(usesMacBrowser({ tools: [{ name: "lightbulb_desktop", args: { action: "shell" } }] })).toBe(false);
  // The retired typed `browser` action no longer marks a Mac run.
  expect(usesMacBrowser({ tools: [{ name: "lightbulb_desktop", args: { action: "browser" } }] })).toBe(false);
  render(<MacBrowserPreview agentName="Friday" runId="run-9" />);
  expect(screen.getByText("Friday is browsing on a Mac")).toBeInTheDocument();
  expect(screen.getAllByTestId("frames").at(-1)).toHaveAttribute("data-run", "run-9");
  expect(screen.getAllByTestId("frames").at(-1)).toHaveAttribute("data-interactive", "false");
  expect(screen.getAllByTestId("frames").at(-1)).toHaveAttribute("data-heartbeat", "/browser/frames");
  expect(screen.getAllByTestId("frames").at(-1)).toHaveAttribute("data-body", JSON.stringify({ tier: "thumb" }));
  expect(screen.getAllByTestId("frames").at(-1)).not.toHaveAttribute("data-mac");
});

it("hiding the page unmounts the Mac thumbnail, so its frames heartbeat stops", async () => {
  const { MacBrowserPreview } = await import("../BrowserRunPreview");
  const setVisibility = (state: DocumentVisibilityState) => act(() => {
    Object.defineProperty(document, "visibilityState", { configurable: true, value: state });
    document.dispatchEvent(new Event("visibilitychange"));
  });
  render(<MacBrowserPreview agentName="Friday" runId="run-9" />);
  expect(screen.getByTestId("frames")).toHaveAttribute("data-body", JSON.stringify({ tier: "thumb" }));
  try {
    setVisibility("hidden");
    expect(screen.queryByTestId("frames")).toBeNull();
    setVisibility("visible");
    expect(screen.getByTestId("frames")).toHaveAttribute("data-run", "run-9");
  } finally {
    setVisibility("visible");
  }
});

it("waits for the first intersection before starting a Mac thumbnail", async () => {
  let observe: ((entries: Array<{ isIntersecting: boolean }>) => void) | undefined;
  vi.stubGlobal("IntersectionObserver", class {
    constructor(callback: (entries: Array<{ isIntersecting: boolean }>) => void) { observe = callback; }
    observe() {}
    disconnect() {}
  });
  try {
    const { MacBrowserPreview } = await import("../BrowserRunPreview");
    render(<MacBrowserPreview agentName="Friday" runId="run-9" />);
    expect(screen.queryByTestId("frames")).toBeNull();
    act(() => observe?.([{ isIntersecting: true }]));
    expect(screen.getByTestId("frames")).toHaveAttribute("data-run", "run-9");
    act(() => observe?.([{ isIntersecting: false }]));
    expect(screen.queryByTestId("frames")).toBeNull();
  } finally {
    vi.unstubAllGlobals();
  }
});

it("clicking the Mac preview (label or picture) shows that run in the Browser panel", async () => {
  const { MacBrowserPreview } = await import("../BrowserRunPreview");
  render(<MacBrowserPreview agentName="Forge" runId="run-7" />);
  fireEvent.click(screen.getByRole("button", { name: "Forge is browsing on a Mac. Show it in the Browser panel" }));
  fireEvent.click(screen.getByRole("button", { name: "Forge is browsing on a Mac" }));
  expect(controller.showMacRunInBrowser).toHaveBeenCalledTimes(2);
  expect(controller.showMacRunInBrowser).toHaveBeenCalledWith("run-7");
  expect(screen.queryByRole("dialog")).toBeNull();
});

it("with no dock (the mobile shell) the click shows the same tab full screen, live and interactive", async () => {
  controller.showMacRunInBrowser.mockReturnValueOnce(false);
  const { MacBrowserPreview } = await import("../BrowserRunPreview");
  render(<MacBrowserPreview agentName="Forge" runId="run-7" deviceId="mac-1" />);
  fireEvent.click(screen.getByRole("button", { name: "Forge is browsing on a Mac" }));
  const dialog = await screen.findByRole("dialog");
  expect(dialog).not.toHaveTextContent(/view only/i);
  const large = dialog.querySelector('[data-testid="frames"]');
  expect(large).toHaveAttribute("data-run", "run-7");
  expect(large).toHaveAttribute("data-mac", "mac-1");
  expect(large).toHaveAttribute("data-prefer", "view");
});

it("a Mac run that never named its device falls back to its thumbnail, view-only", async () => {
  const { MacRunFrames } = await import("../BrowserRunPreview");
  render(<MacRunFrames runId="run-8" />);
  expect(screen.getByTestId("frames")).not.toHaveAttribute("data-mac");
  expect(screen.getByTestId("frames")).toHaveAttribute("data-body", JSON.stringify({ tier: "thumb" }));
});

const doneResult = (obj: unknown) => [{ type: "text", text: JSON.stringify(obj) }];
const useBrowserCall = (status: "running" | "done" | "error", result?: unknown) =>
  ({ name: "lightbulb_desktop", args: { action: "use_browser" }, status, result: result === undefined ? null : doneResult(result) });

describe("usesMacBrowser / macBrowser (use_browser)", () => {
  it("treats a run whose last selection is a successful use_browser as a Mac run", async () => {
    const { usesMacBrowser } = await import("../BrowserRunPreview");
    const run = { tools: [useBrowserCall("done", { browser: "mac", deviceId: "d1", deviceName: "Prasiddha's Mac" })] };
    expect(usesMacBrowser(run)).toBe(true);
  });

  it("a failed or running use_browser is not a Mac run", async () => {
    const { usesMacBrowser, macBrowser } = await import("../BrowserRunPreview");
    const running = { tools: [useBrowserCall("running")] };
    const failed = { tools: [useBrowserCall("done", { error: "No Mac is paired." })] };
    expect(usesMacBrowser(running)).toBe(false);
    expect(usesMacBrowser(failed)).toBe(false);
    expect(macBrowser(running)).toBeUndefined();
    expect(macBrowser(failed)).toBeUndefined();
  });

  it("a run that switched back with use_cloud_browser is not a Mac run", async () => {
    const { usesMacBrowser, macBrowser } = await import("../BrowserRunPreview");
    const run = { tools: [
      useBrowserCall("done", { browser: "mac", deviceId: "d1" }),
      { name: "lightbulb_desktop", args: { action: "use_cloud_browser" }, status: "done", result: doneResult({ browser: "cloud" }) },
    ] };
    expect(usesMacBrowser(run)).toBe(false);
    expect(macBrowser(run)).toBeUndefined();
  });

  it("reads deviceName from the use_browser result", async () => {
    const { macBrowser } = await import("../BrowserRunPreview");
    const run = { tools: [useBrowserCall("done", { browser: "mac", deviceId: "d1", deviceName: "Prasiddha's Mac" })] };
    expect(macBrowser(run)).toEqual({ deviceId: "d1", deviceName: "Prasiddha's Mac" });
    const unnamed = { tools: [useBrowserCall("done", { browser: "mac", deviceId: "d2", deviceName: null })] };
    expect(macBrowser(unnamed)).toEqual({ deviceId: "d2", deviceName: undefined });
  });
});

it("preview label names the Mac", async () => {
  const { MacBrowserPreview } = await import("../BrowserRunPreview");
  render(<MacBrowserPreview agentName="Forge" runId="run-7" deviceName="Prasiddha's Mac" />);
  expect(screen.getByText("Forge is browsing on Prasiddha's Mac")).toBeInTheDocument();
});

it("a Mac run shows only the Mac card, named with its site, never the cloud one too", async () => {
  const { RunBrowserPreview } = await import("../BrowserRunPreview");
  const run = { runId: "run-9", tools: [
    useBrowserCall("done", { browser: "mac", deviceId: "d1", deviceName: "Prasiddha's Mac" }),
    { name: "browser_navigate", args: { url: "https://app.supademo.com/x" }, status: "done", result: null },
  ] } as unknown as Parameters<typeof RunBrowserPreview>[0]["run"];
  render(<RunBrowserPreview agentName="Friday" run={run} />);
  expect(screen.getByText("Friday is browsing app.supademo.com on Prasiddha's Mac")).toBeInTheDocument();
  expect(screen.queryByTestId("browser-run-preview")).toBeNull();
});

it("a Mac card never names a site the run opened in the cloud before switching", async () => {
  const { RunBrowserPreview } = await import("../BrowserRunPreview");
  const run = { runId: "run-10", tools: [
    { name: "browser_navigate", args: { url: "https://cloud-only.example/x" }, status: "done", result: null },
    useBrowserCall("done", { browser: "mac", deviceId: "d1", deviceName: "Prasiddha's Mac" }),
  ] } as unknown as Parameters<typeof RunBrowserPreview>[0]["run"];
  render(<RunBrowserPreview agentName="Friday" run={run} />);
  expect(screen.getByText("Friday is browsing on Prasiddha's Mac")).toBeInTheDocument();
});
